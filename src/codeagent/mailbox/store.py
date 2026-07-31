"""Mailbox store — filesystem operations for session-based direct-inbox."""
from __future__ import annotations

import json
import os
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from codeagent.mailbox.protocol import (
    LEASE_TIMEOUT_S,
    VALID_KINDS,
    VALID_STATES,
    Message,
    StatusSnapshot,
    validate_agent_id,
    validate_message,
)


def resolve_root(root: Optional[Path] = None) -> Path:
    if root is not None:
        return root
    env = os.environ.get("MAILBOX_ROOT")
    if env:
        return Path(env)
    # Default matches original tmux-agent-skills/tools/mailbox
    return Path.home() / "Dropbox" / "logseq" / "pages" / "mi-docs" / ".mailbox"


def gen_msg_id(sender: str) -> str:
    import random
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{sender}_{ts}_{suffix}"


class MailboxStore:
    """Filesystem-backed mailbox store."""

    def __init__(self, root: Optional[Path] = None):
        self.root = resolve_root(root)

    def session_dir(self, session_id: str) -> Path:
        validate_agent_id(session_id)
        return self.root / session_id

    def agent_dir(self, session_id: str, agent_id: str) -> Path:
        validate_agent_id(agent_id)
        return self.session_dir(session_id) / agent_id

    def agent_subdir(self, session_id: str, agent_id: str, sub: str) -> Path:
        return self.agent_dir(session_id, agent_id) / sub

    def list_messages(self, inbox: Path) -> list[Path]:
        if not inbox.exists():
            return []
        return sorted(
            [f for f in inbox.glob("*.json")
             if f.is_file() and not f.is_symlink()
             and not f.name.startswith(".sync-conflict-")
             and not f.name.startswith(".tmp-")],
            key=lambda f: f.stat().st_mtime,
        )

    # ── Session ────────────────────────────────────────────────────────

    def session_init(self, session_id: str, manager_id: str, agent_ids: list[str]) -> str:
        sd = self.session_dir(session_id)
        if sd.exists():
            raise ValueError(f"session already exists: {session_id}")
        sd.mkdir(parents=True)

        meta = {
            "protocol_version": "2",
            "session_id": session_id,
            "manager": manager_id,
            "agents": sorted(set(agent_ids)),
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        tmp = sd / ".tmp-session.json"
        with open(tmp, "w") as f:
            f.write(json.dumps(meta, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(sd / "session.json"))

        for aid in meta["agents"]:
            for sub in ("inbox", "processing", "archive", "_corrupt"):
                self.agent_subdir(session_id, aid, sub).mkdir(parents=True, exist_ok=True)
        for sub in ("inbox", "processing", "archive", "_corrupt"):
            self.agent_subdir(session_id, manager_id, sub).mkdir(parents=True, exist_ok=True)

        return f"session {session_id} created: manager={manager_id}, agents={meta['agents']}"

    # ── Send ───────────────────────────────────────────────────────────

    def send(
        self, session_id: str, from_id: str, to_id: str,
        subject: str, body: str, kind: str = "REPORT",
        reply_to: str = "", run_id: str = "", request_id: str = "",
    ) -> str:
        sd = self.session_dir(session_id)
        if not sd.exists():
            raise ValueError(f"session not found: {session_id}")
        inbox = self.agent_subdir(session_id, to_id, "inbox")
        if not inbox.exists():
            raise ValueError(f"agent not in session: {to_id}")
        if kind not in VALID_KINDS:
            raise ValueError(f"invalid kind: {kind}")

        msg_id = gen_msg_id(from_id)
        while (inbox / f"{msg_id}.json").exists():
            msg_id = gen_msg_id(from_id)

        msg = Message(
            session_id=session_id, from_id=from_id, to_id=to_id,
            subject=subject, body=body, kind=kind, msg_id=msg_id,
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            reply_to=reply_to, run_id=run_id, request_id=request_id,
        )

        ok, reason = validate_message(msg.to_dict(), session_id)
        if not ok:
            raise ValueError(f"send validation failed: {reason}")

        dest = inbox / f"{msg_id}.json"
        tmp = inbox / f".tmp-{msg_id}.json"
        with open(tmp, "w") as f:
            f.write(json.dumps(msg.to_dict(), indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(dest))
        return f"sent → {to_id}/inbox/{msg_id}.json"

    # ── Peek ───────────────────────────────────────────────────────────

    def peek(self, session_id: str, agent_id: str, max_messages: int = 5, max_subject: int = 80) -> dict:
        inbox = self.agent_subdir(session_id, agent_id, "inbox")
        files = self.list_messages(inbox)
        if not files:
            return {"pending": 0, "messages": []}

        summaries = []
        limit = min(len(files), max_messages)
        for f in files[:limit]:
            try:
                msg = json.loads(f.read_bytes())
                summaries.append({
                    "from": msg.get("from", "?"),
                    "kind": msg.get("kind", "?"),
                    "subject": msg.get("subject", "")[:max_subject],
                    "msg_id": msg.get("msg_id", f.stem),
                })
            except (json.JSONDecodeError, UnicodeDecodeError):
                summaries.append({"from": "?", "kind": "?", "subject": "(unreadable)", "msg_id": f.stem})
        return {"pending": len(files), "messages": summaries}

    # ── Read (two-phase consumption) ───────────────────────────────────

    def read(self, session_id: str, agent_id: str, owner: str) -> Optional[dict]:
        """Read oldest message (inbox→processing). Returns message dict or None."""
        inbox = self.agent_subdir(session_id, agent_id, "inbox")
        processing = self.agent_subdir(session_id, agent_id, "processing")
        corrupt_dir = self.agent_subdir(session_id, agent_id, "_corrupt")

        while True:
            files = self.list_messages(inbox)
            if not files:
                return None

            target = files[0]
            try:
                msg = json.loads(target.read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError):
                corrupt_dir.mkdir(parents=True, exist_ok=True)
                os.replace(str(target), str(corrupt_dir / target.name))
                continue

            # Full validation: session + recipient + filename
            ok, reason = validate_message(msg, session_id, agent_id, target.name)
            if not ok:
                corrupt_dir.mkdir(parents=True, exist_ok=True)
                os.replace(str(target), str(corrupt_dir / target.name))
                continue

            processing.mkdir(parents=True, exist_ok=True)
            dest = processing / target.name
            claim_file = processing / f".claim-{target.stem}-{owner}.json"
            tmp_claim = processing / f".tmp-claim-{target.stem}-{owner}.json"

            claim_meta = {
                "owner": owner,
                "claimed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "msg_id": target.stem,
            }
            with open(tmp_claim, "w") as fc:
                fc.write(json.dumps(claim_meta))
                fc.flush()
                os.fsync(fc.fileno())
            os.replace(str(tmp_claim), str(claim_file))

            try:
                os.replace(str(target), str(dest))
            except OSError:
                claim_file.unlink(missing_ok=True)
                continue

            return msg

    # ── Finalize ───────────────────────────────────────────────────────

    @staticmethod
    def _validate_msg_id(msg_id: str) -> None:
        if "/" in msg_id or "\\" in msg_id or ".." in msg_id:
            raise ValueError(f"invalid msg_id: {msg_id}")

    def finalize(self, session_id: str, agent_id: str, msg_id: str, owner: str) -> str:
        self._validate_msg_id(msg_id)
        processing = self.agent_subdir(session_id, agent_id, "processing")
        archive = self.agent_subdir(session_id, agent_id, "archive")
        target = processing / f"{msg_id}.json"

        claim_files = sorted(processing.glob(f".claim-{msg_id}-*.json"))
        if not claim_files:
            raise ValueError(f"no claim file for {msg_id}")
        if len(claim_files) > 1:
            raise ValueError(f"multiple claim files for {msg_id}")
        claim_file = claim_files[0]

        if not target.exists():
            raise ValueError(f"msg not in processing/: {msg_id}")

        claim = json.loads(claim_file.read_bytes())
        if claim.get("owner") != owner:
            raise ValueError(f"owner mismatch: claim={claim.get('owner')} vs {owner}")

        archive.mkdir(parents=True, exist_ok=True)
        os.replace(str(target), str(archive / target.name))
        claim_file.unlink(missing_ok=True)
        return f"finalized → archive/{target.name}"

    # ── Release ────────────────────────────────────────────────────────

    def release(self, session_id: str, agent_id: str, msg_id: str, owner: str) -> str:
        self._validate_msg_id(msg_id)
        inbox = self.agent_subdir(session_id, agent_id, "inbox")
        processing = self.agent_subdir(session_id, agent_id, "processing")
        target = processing / f"{msg_id}.json"

        claim_files = sorted(processing.glob(f".claim-{msg_id}-*.json"))
        if not target.exists():
            raise ValueError(f"msg not found in processing/: {msg_id}")
        if len(claim_files) > 1:
            raise ValueError(f"multiple claim files for {msg_id}")

        claim_file = claim_files[0] if claim_files else None
        if claim_file and claim_file.exists():
            claim = json.loads(claim_file.read_bytes())
            if claim.get("owner") != owner:
                raise ValueError(f"owner mismatch on release: claim={claim.get('owner')} vs {owner}")

        os.replace(str(target), str(inbox / target.name))
        if claim_file:
            claim_file.unlink(missing_ok=True)
        return f"released → inbox/{target.name}"

    # ── Recover stale ──────────────────────────────────────────────────

    def recover_stale(self, session_id: str, agent_id: str) -> str:
        processing_dir = self.agent_subdir(session_id, agent_id, "processing")
        inbox = self.agent_subdir(session_id, agent_id, "inbox")
        if not processing_dir.exists():
            return "no processing/ directory"

        recovered = 0
        cutoff = datetime.now(timezone.utc).timestamp() - LEASE_TIMEOUT_S
        for cf in sorted(processing_dir.glob(".claim-*.json")):
            try:
                claim = json.loads(cf.read_bytes())
                claimed_at_s = claim.get("claimed_at", "")
                if claimed_at_s:
                    claimed_ts = datetime.fromisoformat(claimed_at_s).timestamp()
                    if claimed_ts < cutoff:
                        msg_id = claim.get("msg_id") or cf.stem.replace(".claim-", "", 1).split("-", 1)[0]
                        msg_file = processing_dir / f"{msg_id}.json"
                        if msg_file.exists():
                            os.replace(str(msg_file), str(inbox / msg_file.name))
                            cf.unlink()
                            recovered += 1
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass
        return f"recovered {recovered} stale claim(s)"

    # ── Status ─────────────────────────────────────────────────────────

    def write_status(self, session_id: str, agent_id: str, state: str, current_task: str = "", last_conclusion: str = "") -> str:
        if state not in VALID_STATES:
            raise ValueError(f"invalid state: {state}")
        # Verify session and agent exist
        sd = self.session_dir(session_id)
        if not sd.exists():
            raise ValueError(f"session not found: {session_id}")
        ad = self.agent_dir(session_id, agent_id)
        if not ad.exists():
            raise ValueError(f"agent not in session: {agent_id}")

        status = StatusSnapshot(
            session_id=session_id, state=state,
            current_task=current_task, last_conclusion=last_conclusion,
            updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        dest = ad / "status.json"
        tmp = ad / ".tmp-status.json"
        with open(tmp, "w") as f:
            f.write(json.dumps(status.to_dict(), indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(dest))
        return f"status: {state}"

    def read_status(self, session_id: str, agent_id: str) -> Optional[StatusSnapshot]:
        status_file = self.agent_dir(session_id, agent_id) / "status.json"
        if not status_file.exists():
            return None
        try:
            d = json.loads(status_file.read_bytes())
            return StatusSnapshot.from_dict(d)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # ── Stats / Clear ──────────────────────────────────────────────────

    def stats(self, session_id: str, agent_id: str) -> dict[str, int]:
        ad = self.agent_dir(session_id, agent_id)
        return {d: len(self.list_messages(ad / d)) for d in ("inbox", "processing", "archive", "_corrupt")}

    def clear(self, session_id: str, agent_id: str, *, prune_stale: bool = False) -> str:
        ad = self.agent_dir(session_id, agent_id)
        total = 0
        for sub in ("archive", "_corrupt"):
            d = ad / sub
            if d.exists():
                for f in d.glob("*.json"):
                    f.unlink()
                    total += 1
        if prune_stale:
            self.recover_stale(session_id, agent_id)
        return f"cleared {total}"

    # ── Check (legacy) ─────────────────────────────────────────────────

    def check(self, session_id: str, agent_id: str, max_messages: int = 0) -> list[dict]:
        """Legacy: validate + archive from inbox only (no processing/ awareness)."""
        inbox = self.agent_subdir(session_id, agent_id, "inbox")
        archive = self.agent_subdir(session_id, agent_id, "archive")
        corrupt_dir = self.agent_subdir(session_id, agent_id, "_corrupt")

        files = self.list_messages(inbox)
        if not files:
            return []

        results = []
        limit = max_messages or len(files)
        for entry in files[:limit]:
            filename = entry.name
            try:
                msg = json.loads(entry.read_bytes())
                ok, reason = validate_message(msg, expected_agent=agent_id, filename=filename)
                if not ok:
                    raise ValueError(reason)
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
                corrupt_dir.mkdir(parents=True, exist_ok=True)
                os.replace(str(entry), str(corrupt_dir / filename))
                continue

            archive.mkdir(parents=True, exist_ok=True)
            os.replace(str(entry), str(archive / filename))
            results.append(msg)

        return results
