"""Mailbox store — filesystem operations for session-based direct-inbox."""
from __future__ import annotations

import json
import os
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from codeagent.constants import LEASE_TIMEOUT_S, MAX_MAILBOX_BODY
from codeagent.mailbox.protocol import (
    BROADCAST_TO,
    VALID_KINDS,
    VALID_STATES,
    AttachmentRef,
    Message,
    StatusSnapshot,
    attachment_error,
    validate_agent_id,
    validate_message,
)


def resolve_root(root: Optional[Path] = None) -> Path:
    if root is not None:
        return root
    env = os.environ.get("MAILBOX_ROOT")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(xdg) / "codeagent" / "mailbox"


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
        # Validate all IDs before creating anything
        validate_agent_id(session_id)
        validate_agent_id(manager_id)
        for aid in agent_ids:
            validate_agent_id(aid)

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

    def read_session(self, session_id: str) -> Optional[dict]:
        """Read session.json metadata."""
        session_file = self.session_dir(session_id) / "session.json"
        try:
            if not session_file.exists():
                return None
            return json.loads(session_file.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None

    # ── Send ───────────────────────────────────────────────────────────

    def send(
        self, session_id: str, from_id: str, to_id: str,
        subject: str, body: str, kind: str = "REPORT",
        reply_to: str = "", run_id: str = "", request_id: str = "",
        attachments: Optional[list] = None,
        msg_id: Optional[str] = None,
    ) -> str:
        """Deliver a message. ``to_id == "*"`` broadcasts to every roster
        member except the sender (same msg_id, one envelope per inbox).
        Every recipient is validated before any file is written, and a
        single canonical history record is appended on success.
        """
        sd = self.session_dir(session_id)
        if not sd.exists():
            raise ValueError(f"session not found: {session_id}")

        # Validate sender is in roster
        meta = self.read_session(session_id)
        if meta is None:
            raise ValueError(f"session metadata not found or corrupt: {session_id}")
        roster = {meta.get("manager", "")} | set(meta.get("agents", []))
        if from_id not in roster:
            raise ValueError(f"sender not in roster: {from_id}")

        # Validate attachment refs (independent of recipients)
        refs: list[AttachmentRef] = []
        if attachments is not None:
            if not isinstance(attachments, list):
                raise ValueError("attachments must be a list")
            for att in attachments:
                ad = att.to_dict() if isinstance(att, AttachmentRef) else att
                if not isinstance(ad, dict):
                    raise ValueError("invalid attachment: must be AttachmentRef or dict")
                err = attachment_error(ad)
                if err is not None:
                    raise ValueError(f"invalid attachment: {err}")
                refs.append(AttachmentRef.from_dict(ad))

        # Resolve recipients; validate ALL of them before writing anything
        if to_id == BROADCAST_TO:
            is_broadcast = True
            recipients = sorted(roster - {from_id})
        else:
            is_broadcast = False
            recipients = [to_id]
            if to_id not in roster:
                raise ValueError(f"recipient not in roster: {to_id}")
        for rid in recipients:
            if not self.agent_subdir(session_id, rid, "inbox").exists():
                raise ValueError(f"agent not in session: {rid}")

        if kind not in VALID_KINDS:
            raise ValueError(f"invalid kind: {kind}")
        if isinstance(body, str) and len(body.encode("utf-8")) > MAX_MAILBOX_BODY:
            raise ValueError(f"body exceeds {MAX_MAILBOX_BODY}-byte limit")

        if msg_id is not None:
            self._validate_msg_id(msg_id)
            # Idempotency: reject if msg_id already exists in any recipient inbox or history
            hd = sd / "history"
            if any(
                (self.agent_subdir(session_id, rid, "inbox") / f"{msg_id}.json").exists()
                for rid in recipients
            ) or (hd / f"{msg_id}.json").exists():
                raise ValueError(f"msg_id already exists: {msg_id}")
        else:
            msg_id = gen_msg_id(from_id)
            while any(
                (self.agent_subdir(session_id, rid, "inbox") / f"{msg_id}.json").exists()
                for rid in recipients
            ):
                msg_id = gen_msg_id(from_id)

        msg = Message(
            session_id=session_id, from_id=from_id, to_id=to_id,
            subject=subject, body=body, kind=kind, msg_id=msg_id,
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            reply_to=reply_to, run_id=run_id, request_id=request_id,
            attachments=refs,
        )

        ok, reason = validate_message(msg.to_dict(), session_id)
        if not ok:
            raise ValueError(f"send validation failed: {reason}")

        # All recipients validated — now write every envelope, then history
        payload = msg.to_dict()
        for rid in recipients:
            inbox = self.agent_subdir(session_id, rid, "inbox")
            dest = inbox / f"{msg_id}.json"
            tmp = inbox / f".tmp-{msg_id}.json"
            with open(tmp, "w") as f:
                f.write(json.dumps(payload, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(dest))

        self.append_history(session_id, payload)

        if is_broadcast:
            return f"broadcast → {len(recipients)} recipients"
        return f"sent → {to_id}/inbox/{msg_id}.json"

    # ── Canonical history (append-only, shared across the session) ────

    def history_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "history"

    def append_history(self, session_id: str, message: dict) -> str:
        """Append one canonical history record.

        Append-only: one file per msg_id, written via O_EXCL tmp + atomic
        rename so a duplicate msg_id can never overwrite an existing record.
        Independent of per-recipient archives — a broadcast appends exactly
        one record for the whole swarm.
        """
        sd = self.session_dir(session_id)
        if not sd.exists():
            raise ValueError(f"session not found: {session_id}")
        if not isinstance(message, dict):
            raise ValueError("history message must be a dict")
        ok, reason = validate_message(message, session_id)
        if not ok:
            raise ValueError(f"history validation failed: {reason}")
        self._validate_msg_id(message["msg_id"])

        hd = sd / "history"
        hd.mkdir(parents=True, exist_ok=True)
        dest = hd / f"{message['msg_id']}.json"
        if dest.exists():
            raise ValueError(f"history entry already exists: {message['msg_id']}")
        tmp = hd / f".tmp-{message['msg_id']}.json"
        try:
            with open(tmp, "x") as f:  # O_EXCL: concurrent duplicate appends fail
                f.write(json.dumps(message, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError:
            raise ValueError(f"history entry already exists: {message['msg_id']}")
        os.replace(str(tmp), str(dest))
        return f"history: {message['msg_id']}"

    def read_history(
        self, session_id: str,
        since: Optional[str] = None,
        before: Optional[str] = None,
        limit: Optional[int] = None,
        from_id: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> list[dict]:
        """Read canonical history, newest first.

        Filters (all optional, combined with AND):
          since  — only messages with created_at >= since
          before — only messages with created_at < before
          limit  — cap the number of returned records
          from   — only messages from this sender
          kind   — only messages of this kind
        """
        if kind is not None and kind not in VALID_KINDS:
            raise ValueError(f"invalid kind: {kind}")
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")

        files = self.list_messages(self.history_dir(session_id))
        out = []
        for f in files:
            try:
                msg = json.loads(f.read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue
            ok, reason = validate_message(msg, session_id, filename=f.name)
            if not ok:
                continue  # skip corrupt/foreign entries; history is append-only
            if since is not None and msg["created_at"] < since:
                continue
            if before is not None and msg["created_at"] >= before:
                continue
            if from_id is not None and msg["from"] != from_id:
                continue
            if kind is not None and msg["kind"] != kind:
                continue
            out.append(msg)

        out.sort(key=lambda m: (m["created_at"], m["msg_id"]), reverse=True)
        if limit is not None:
            out = out[:limit]
        return out

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
            # Unique tmp claim to avoid concurrent collision
            import random as _rand
            nonce = _rand.randint(1000, 9999)
            claim_file = processing / f".claim-{target.stem}-{owner}.json"
            tmp_claim = processing / f".tmp-claim-{target.stem}-{owner}-{nonce}.json"

            claim_meta = {
                "owner": owner,
                "claimed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "msg_id": target.stem,
            }
            try:
                with open(tmp_claim, "x") as fc:  # O_EXCL: fail if exists
                    fc.write(json.dumps(claim_meta))
                    fc.flush()
                    os.fsync(fc.fileno())
            except FileExistsError:
                tmp_claim.unlink(missing_ok=True)
                continue

            try:
                os.replace(str(tmp_claim), str(claim_file))
                os.replace(str(target), str(dest))
            except OSError:
                tmp_claim.unlink(missing_ok=True)
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

    def finalize_from_inbox(self, session_id: str, agent_id: str, msg_id: str, owner: str) -> str:
        """Finalize a message directly (no claim file required).

        Used by SwarmReceiver for auto-consumption: the receiver writes
        messages directly to inbox and acks immediately, so the normal
        two-phase read→processing→finalize flow never runs.

        Also handles messages already moved to processing/ by a prior
        ``read()`` (two-phase consumers): archive from whichever location
        the message is in.
        """
        self._validate_msg_id(msg_id)
        inbox = self.agent_subdir(session_id, agent_id, "inbox")
        processing = self.agent_subdir(session_id, agent_id, "processing")
        archive = self.agent_subdir(session_id, agent_id, "archive")

        src = inbox / f"{msg_id}.json"
        if not src.exists():
            src = processing / f"{msg_id}.json"
        if not src.exists():
            raise ValueError(f"msg not in inbox/ or processing/: {msg_id}")

        archive.mkdir(parents=True, exist_ok=True)
        os.replace(str(src), str(archive / src.name))
        return f"finalized → archive/{src.name}"

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
        try:
            if not status_file.exists():
                return None
            d = json.loads(status_file.read_bytes())
            # Strict validation: exactly 5 required keys, all strings
            required = {"session_id", "state", "current_task", "last_conclusion", "updated_at"}
            if not isinstance(d, dict) or set(d.keys()) != required:
                return None
            if not all(isinstance(d[k], str) for k in required):
                return None
            if d["state"] not in VALID_STATES:
                return None
            if d["session_id"] != session_id:
                return None
            return StatusSnapshot.from_dict(d)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None

    # ── Stats / Clear ──────────────────────────────────────────────────

    def stats(self, session_id: str, agent_id: str) -> dict[str, int]:
        ad = self.agent_dir(session_id, agent_id)
        return {d: len(self.list_messages(ad / d)) for d in ("inbox", "processing", "archive", "_corrupt")}

    def clear(self, session_id: str, agent_id: str, *, prune_stale: bool = False) -> str:
        """Clear archive only. Use purge() for _corrupt."""
        ad = self.agent_dir(session_id, agent_id)
        total = 0
        d = ad / "archive"
        if d.exists():
            for f in d.glob("*.json"):
                f.unlink()
                total += 1
        if prune_stale:
            self.recover_stale(session_id, agent_id)
        return f"cleared {total}"

    def purge(self, session_id: str, agent_id: str) -> str:
        """Purge _corrupt (destructive — audit files are lost)."""
        ad = self.agent_dir(session_id, agent_id)
        total = 0
        d = ad / "_corrupt"
        if d.exists():
            for f in d.glob("*.json"):
                f.unlink()
                total += 1
        return f"purged {total}"

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
