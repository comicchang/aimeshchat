"""Mailbox store — filesystem operations for session-based direct-inbox."""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import string
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from codeagent.constants import (
    ISO_TIMESTAMP_FORMAT,
    LEASE_CLOCK_TOLERANCE_S,
    LEASE_TIMEOUT_S,
    MAX_ATTACHMENT_SIZE,
    MAX_MAILBOX_BODY,
    MSG_ID_TIMESTAMP_FORMAT,
    SEQ_WIDTH,
    STREAM_CURSOR_FILE,
    STREAM_CURSOR_INITIAL,
)
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
    validate_path_component,
)

log = logging.getLogger(__name__)

# P1-6: mailbox 敏感数据权限收敛 —— 目录 0700、消息文件 0600。
# 对照既有收敛（control socket 0700、identity.json 0600），避免消息体
# 以 0644/0755 落盘被同机其他用户窥探。
_DIR_MODE_0700 = 0o700
_FILE_MODE_0600 = 0o600


def _mkdir_0700(d: Path) -> None:
    """P1-6: 保留原 mkdir（parents/exist_ok）语义，追加 chmod 0700。"""
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, _DIR_MODE_0700)
    except OSError:
        pass  # 非 POSIX / 只读挂载 —— 尽力而为


def _chmod_0600(p: Path) -> None:
    """P1-6: 消息体/元数据文件权限收紧为 0600。"""
    try:
        os.chmod(p, _FILE_MODE_0600)
    except OSError:
        pass  # 非 POSIX —— 尽力而为


def resolve_root(root: Optional[Path] = None) -> Path:
    if root is not None:
        return root
    env = os.environ.get("MAILBOX_ROOT")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(xdg) / "aimeshchat" / "mailbox"


def gen_msg_id(sender: str) -> str:
    import random
    ts = datetime.now(timezone.utc).strftime(MSG_ID_TIMESTAMP_FORMAT)
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{sender}_{ts}_{suffix}"


def _attachments_eq(a: list, b: list) -> bool:
    """P3-2: order-insensitive attachment comparison.

    Two attachment lists are equal when they contain the same refs in any
    order. Each ref is normalized to a sorted ``(key, value)`` tuple so
    dict key order never matters.
    """
    def _norm(ref) -> tuple:
        if isinstance(ref, dict):
            return tuple(sorted((k, str(v)) for k, v in ref.items()))
        if isinstance(ref, AttachmentRef):
            return tuple(sorted((k, str(v)) for k, v in ref.to_dict().items()))
        return (str(ref),)

    return len(a) == len(b) and {_norm(x) for x in a} == {_norm(x) for x in b}


class ParkLeaseActiveError(Exception):
    """Cannot clear mailbox while a park lease is active."""


class StatusFileCorruptError(Exception):
    """P2-5: status.json exists but is corrupt/unparseable.

    Raised by ``read_status(..., strict=True)`` so callers can tell a
    corrupt status file apart from an absent one (which yields ``None``).
    """

    def __init__(self, path: Path, reason: str):
        self.path = str(path)
        self.reason = reason
        super().__init__(f"corrupt status file {self.path}: {reason}")


try:
    from codeagent.park.registry import ParkRegistry
except ImportError:
    ParkRegistry = None  # type: ignore[assignment,misc]


class MailboxStore:
    """Filesystem-backed mailbox store."""

    def __init__(self, root: Optional[Path] = None):
        self.root = resolve_root(root)

    # ── Durability / mutual-exclusion helpers ─────────────────────────

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        """P2-4: fsync a directory so a rename inside it survives power loss.

        ``os.replace`` only updates the directory entry — without a parent
        directory fsync the rename may be lost on power failure.
        """
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return  # dir gone or not openable (e.g. Windows) — nothing to persist
        try:
            os.fsync(fd)
        except OSError:
            pass  # some filesystems refuse dir fsync — best effort
        finally:
            os.close(fd)

    @staticmethod
    @contextlib.contextmanager
    def _claim_lock(processing: Path, msg_id: str, owner: str):
        """P2-2: exclusive per-claim flock across claim read-decide-move cycles.

        Held by ``recover_stale()``, ``renew_claim()`` and ``read()``'s reap
        path so a lease-boundary TOCTOU cannot recycle an actively renewed
        task. Uses a stable lock file (``.claimlock-{msg_id}-{owner}.lock``)
        that is never ``os.replace()``d — the claim file itself is swapped in
        place by ``renew_claim()``, which would invalidate an flock on its
        inode. On platforms without flock (ImportError/OSError) it degrades
        to no exclusion, matching the codebase's existing degrade pattern.
        """
        lock_path = processing / f".claimlock-{msg_id}-{owner}.lock"
        fd = None
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        except OSError:
            fd = None  # cannot create lock file — proceed without exclusion
        try:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass  # non-POSIX / flock unavailable — degrade
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                os.close(fd)

    # ── Park lease guard ────────────────────────────────────────────

    @staticmethod
    def _check_park_lease(session_id: str, agent_id: str) -> None:
        """Raise ParkLeaseActiveError if any active park lease references this agent."""
        if ParkRegistry is None:
            return
        try:
            registry = ParkRegistry()
            from codeagent.domain.park import Lifecycle
            manifest = registry.lookup_by_field("mailbox_agent_id", agent_id)
            if manifest is not None and manifest.lifecycle == Lifecycle.HOT_PARKED:
                raise ParkLeaseActiveError(
                    f"active park lease for {agent_id} in session {session_id}"
                )
        except ParkLeaseActiveError:
            raise
        except Exception:
            # Park registry unavailable or corrupt — don't block cleanup
            pass

    def session_dir(self, session_id: str) -> Path:
        validate_agent_id(session_id)
        return self.root / session_id

    def agent_dir(self, session_id: str, agent_id: str) -> Path:
        validate_agent_id(agent_id)
        return self.session_dir(session_id) / agent_id

    def agent_subdir(self, session_id: str, agent_id: str, sub: str) -> Path:
        return self.agent_dir(session_id, agent_id) / sub

    def _ensure_agent_dirs(self, session_id: str, agent_id: str) -> None:
        """P1-6: 创建 agent 目录及其 inbox/processing/archive/_corrupt 子目录，
        全部收敛 0700（保留原 parents=True/exist_ok mkdir 语义）。"""
        _mkdir_0700(self.agent_dir(session_id, agent_id))
        for sub in ("inbox", "processing", "archive", "_corrupt"):
            _mkdir_0700(self.agent_subdir(session_id, agent_id, sub))

    def list_messages(self, inbox: Path) -> list[Path]:
        if not inbox.exists():
            return []
        # P2-7: a concurrent reader may claim (move) a message between the
        # glob and the stat — skip files that vanish mid-scan instead of
        # crashing the caller.
        found: list[tuple[float, Path]] = []
        for f in inbox.glob("*.json"):
            if not f.is_file() or f.is_symlink():
                continue
            if f.name.startswith(".sync-conflict-") or f.name.startswith(".tmp-"):
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue  # claimed/moved between glob and stat
            found.append((mtime, f))
        return [f for _, f in sorted(found, key=lambda t: t[0])]

    # ── Session ────────────────────────────────────────────────────────

    def session_init(self, session_id: str, manager_id: str, agent_ids: list[str],
                     acl: Optional[dict] = None,
                     execution_modes: Optional[dict[str, str]] = None,
                     return_modes: Optional[dict[str, str]] = None) -> str:
        """B4-Manifest: acl dict 可选——与 roster 一起持久化到 session.json
        （权威副本），供远端 ensure 同步。每次写递增 manifest_revision。"""
        # Validate all IDs before creating anything
        validate_agent_id(session_id)
        validate_agent_id(manager_id)
        for aid in agent_ids:
            validate_agent_id(aid)

        sd = self.session_dir(session_id)
        # P3-3: ONE lock serializes both the merge and the fresh-creation
        # paths. The dir is created up front (exist_ok) so the lock file has
        # a home, and the lock is acquired BEFORE any existence check — a
        # merger can no longer beat a creator into the same dir and crash on
        # a missing session.json, nor can two creators double-write it.
        _mkdir_0700(sd)  # P1-6: 会话目录 0700
        _mkdir_0700(self.root)  # P1-6: mailbox 根目录由 parents=True 隐式创建，一并收敛 0700
        lock_fd = os.open(str(sd / ".session.lock"), os.O_CREAT | os.O_RDWR, _FILE_MODE_0600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass  # non-POSIX — degrade to atomic replace

            existing = self.read_session(session_id)
            if existing is not None:
                # ── Idempotent: merge new agents into existing session ────
                old_manager = existing.get("manager", "")
                if old_manager and old_manager != manager_id:
                    raise ValueError(
                        f"session {session_id} already has manager={old_manager!r}, "
                        f"cannot reassign to manager={manager_id!r}"
                    )

                old_agents = set(existing.get("agents", []))
                new_agents = sorted(set(agent_ids))
                merged = sorted(old_agents | set(new_agents))
                added = sorted(set(new_agents) - old_agents)

                # Manifest revision bumps on every control-plane write
                revision = int(existing.get("manifest_revision", 0)) + 1

                if not added and acl is None and execution_modes is None and return_modes is None:
                    # Nothing new — still bump revision? No: no-op if nothing changed.
                    return f"session {session_id} ok (merged 0 agents)"

                if added:
                    # Create subdirs for newly added agents only
                    for aid in added:
                        self._ensure_agent_dirs(session_id, aid)

                # Rewrite session.json with merged roster (+acl if provided)
                existing["agents"] = merged
                existing["manifest_revision"] = revision
                if acl is not None:
                    existing["acl"] = acl
                if execution_modes is not None:
                    existing_em = existing.get("execution_modes", {})
                    existing_em.update(execution_modes)
                    existing["execution_modes"] = existing_em
                if return_modes is not None:
                    existing_rm = existing.get("return_modes", {})
                    existing_rm.update(return_modes)
                    existing["return_modes"] = existing_rm
                tmp = sd / ".tmp-session.json"
                with open(tmp, "w") as f:
                    f.write(json.dumps(existing, indent=2, ensure_ascii=False))
                    f.flush()
                    os.fsync(f.fileno())
                _chmod_0600(tmp)  # P1-6: session.json 0600（含 acl 等敏感元数据）
                os.replace(str(tmp), str(sd / "session.json"))
                _chmod_0600(sd / "session.json")  # P1-6: 覆盖既有文件的旧权限
                self._fsync_dir(sd)  # P2-4

                if added:
                    return f"session {session_id} ok (merged {len(added)} agents)"
                return f"session {session_id} ok (acl updated)"

            # ── Fresh creation ─────────────────────────────────────────────
            meta = {
                "protocol_version": "2",
                "session_id": session_id,
                "manager": manager_id,
                "agents": sorted(set(agent_ids)),
                "manifest_revision": 1,
                "created_at": datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
            }
            if acl is not None:
                meta["acl"] = acl
            if execution_modes is not None:
                meta["execution_modes"] = execution_modes
            if return_modes is not None:
                meta["return_modes"] = return_modes
            tmp = sd / ".tmp-session.json"
            with open(tmp, "w") as f:
                f.write(json.dumps(meta, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            _chmod_0600(tmp)  # P1-6: session.json 0600
            os.replace(str(tmp), str(sd / "session.json"))
            _chmod_0600(sd / "session.json")  # P1-6
            self._fsync_dir(sd)  # P2-4

            for aid in meta["agents"]:
                self._ensure_agent_dirs(session_id, aid)
            self._ensure_agent_dirs(session_id, manager_id)

            return f"session {session_id} created: manager={manager_id}, agents={meta['agents']}"
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            os.close(lock_fd)

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
        command_id: str = "",  # P1-1: 关联键（gateway 写 command 消息时 = request_id）
        trace_id: str = "", causation_id: str = "",
        attachments: Optional[list] = None,
        msg_id: Optional[str] = None,
        require_ack: bool = False,
        receipt_type: str = "",
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
                if isinstance(ad.get("size"), int) and ad["size"] > MAX_ATTACHMENT_SIZE:
                    raise ValueError(
                        f"attachment exceeds {MAX_ATTACHMENT_SIZE}-byte limit: {ad.get('path', '?')}"
                    )
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
            # P0-a idempotent replay: a message already written (inbox or
            # history) is NOT an error per se — crash/response-loss after the
            # inbox write but before the sender's .delivered marker makes
            # flush() replay the same msg_id. Identical payload → replay
            # succeeds (idempotent); conflicting payload → raise.
            hd = sd / "history"
            existing: Optional[dict] = None
            # P3-2: scan ALL recipient inboxes, not just the first — a crash
            # between per-recipient writes can leave the message in a later
            # inbox only; the old `break` missed it and replayed a duplicate.
            for rid in recipients:
                inbox = self.agent_subdir(session_id, rid, "inbox")
                p = inbox / f"{msg_id}.json"
                if p.exists() and existing is None:
                    try:
                        existing = json.loads(p.read_bytes())
                    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                        existing = {}
            if existing is None:
                hp = hd / f"{msg_id}.json"
                if hp.exists():
                    try:
                        existing = json.loads(hp.read_bytes())
                    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                        existing = {}
            if existing is not None:
                # P2-9: the replay-equality check must cover every v2 field —
                # a replay that silently dropped require_ack/reply_to/run_id/
                # request_id/receipt_type/etc. would otherwise be accepted as
                # "identical" and the ack/correlation semantics lost.
                same = (
                    existing.get("session_id") == session_id
                    and existing.get("from") == from_id
                    and existing.get("to") == to_id
                    and existing.get("subject") == subject
                    and existing.get("body") == body
                    and existing.get("kind") == kind
                    and existing.get("reply_to", "") == (reply_to or "")
                    and existing.get("run_id", "") == (run_id or "")
                    and existing.get("request_id", "") == (request_id or "")
                    and existing.get("command_id", "") == (command_id or "")
                    and existing.get("trace_id", "") == (trace_id or "")
                    and existing.get("causation_id", "") == (causation_id or "")
                    and bool(existing.get("require_ack", False)) == bool(require_ack)
                    and existing.get("receipt_type", "") == (receipt_type or "")
                    # P3-2: compare attachments as unordered sets — the same
                    # attachment list in a different order is semantically
                    # identical, and the old == was order-sensitive.
                    and _attachments_eq(existing.get("attachments", []),
                                        [a.to_dict() for a in refs])
                )
                if not same:
                    raise ValueError(f"msg_id already exists with different payload: {msg_id}")
                # P2-8: identical replay — the message is semantically
                # delivered, but a crash between the per-recipient inbox
                # writes and the history append leaves SOME recipients
                # missing. Backfill them idempotently (skip written, write
                # missing) instead of returning early on the first hit.
                backfilled = 0
                for rid in recipients:
                    inbox = self.agent_subdir(session_id, rid, "inbox")
                    dest = inbox / f"{msg_id}.json"
                    if dest.exists():
                        continue
                    tmp = inbox / f".tmp-{msg_id}.json"
                    with open(tmp, "w") as f:
                        f.write(json.dumps(existing, indent=2, ensure_ascii=False))
                        f.flush()
                        os.fsync(f.fileno())
                    _chmod_0600(tmp)  # P1-6: 消息体信封 0600
                    os.replace(str(tmp), str(dest))
                    _chmod_0600(dest)  # P1-6: 覆盖既有文件的旧权限
                    backfilled += 1
                # P2-4: backfilled envelopes durable before returning.
                for rid in recipients:
                    self._fsync_dir(self.agent_subdir(session_id, rid, "inbox"))
                if not (hd / f"{msg_id}.json").exists():
                    self.append_history(session_id, existing)
                return f"sent → {to_id}/inbox/{msg_id}.json (idempotent replay, backfilled {backfilled})"
        else:
            msg_id = gen_msg_id(from_id)
            # P3-j: collision check must also cover history — a msg_id
            # that exists only in history (e.g. broadcast where some inboxes
            # were cleaned) would silently collide if we only check inbox.
            hd = sd / "history"
            while any(
                (self.agent_subdir(session_id, rid, "inbox") / f"{msg_id}.json").exists()
                for rid in recipients
            ) or (hd / f"{msg_id}.json").exists():
                msg_id = gen_msg_id(from_id)

        msg = Message(
            session_id=session_id, from_id=from_id, to_id=to_id,
            subject=subject, body=body, kind=kind, msg_id=msg_id,
            created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
            reply_to=reply_to, run_id=run_id, request_id=request_id,
            command_id=command_id,
            trace_id=trace_id, causation_id=causation_id,
            attachments=refs,
            require_ack=require_ack,
            receipt_type=receipt_type,
        )

        ok, reason = validate_message(msg.to_dict(), session_id)
        if not ok:
            raise ValueError(f"send validation failed: {reason}")

        # All recipients validated — now write every envelope, then history.
        # P3-k: advance cursor AFTER all envelopes land on disk so a crash
        # between cursor bump and envelope write cannot leave a cursor gap.
        # Step 1: write envelopes without _cursor.
        # Step 2: advance cursor, rewrite envelopes with cursor stamp.
        payload = msg.to_dict()
        for rid in recipients:
            inbox = self.agent_subdir(session_id, rid, "inbox")
            dest = inbox / f"{msg_id}.json"
            tmp = inbox / f".tmp-{msg_id}.json"
            with open(tmp, "w") as f:
                f.write(json.dumps(payload, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            _chmod_0600(tmp)  # P1-6: 消息体信封 0600
            os.replace(str(tmp), str(dest))
            _chmod_0600(dest)  # P1-6: 覆盖既有文件的旧权限
        # P2-4: envelope renames must be durable before the cursor — which
        # readers trust for ordering — is committed.
        for rid in recipients:
            self._fsync_dir(self.agent_subdir(session_id, rid, "inbox"))

        # P3-k: cursor committed only after envelopes are durable
        payload['_cursor'] = self.advance_cursor(session_id)
        for rid in recipients:
            inbox = self.agent_subdir(session_id, rid, "inbox")
            dest = inbox / f"{msg_id}.json"
            tmp = inbox / f".tmp-{msg_id}.json"
            with open(tmp, "w") as f:
                f.write(json.dumps(payload, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            _chmod_0600(tmp)  # P1-6: 消息体信封 0600
            os.replace(str(tmp), str(dest))
            _chmod_0600(dest)  # P1-6: 覆盖既有文件的旧权限
        # P2-4: cursor-stamped envelopes durable before history is appended.
        for rid in recipients:
            self._fsync_dir(self.agent_subdir(session_id, rid, "inbox"))

        self.append_history(session_id, payload)

        if is_broadcast:
            return f"broadcast → {len(recipients)} recipients"
        return f"sent → {to_id}/inbox/{msg_id}.json"

    # ── Stream cursor ─────────────────────────────────────────────────

    def advance_cursor(self, session_id: str) -> str:
        """Advance and return the opaque stream cursor for a session.

        Reads ``<session_dir>/.stream-cursor`` JSON ``{'epoch_ms':N,'seq':N}``,
        increments seq (bumps epoch_ms and resets seq if epoch_ms changed),
        writes back atomically, returns ``"epoch_ms/seq"``.

        B2/P0-b: seq is zero-padded to a fixed width (``SEQ_WIDTH``) so the
        cursor stays lexicographically ordered within one epoch ("10" would
        otherwise sort before "9"), and the read-modify-write is guarded by
        a cross-process ``flock`` so concurrent senders cannot produce
        duplicate seq values.
        """
        sd = self.session_dir(session_id)
        _mkdir_0700(sd)  # P1-6: 会话目录 0700
        cursor_file = sd / STREAM_CURSOR_FILE

        now_ms = int(time.time() * 1000)

        lock_fd = os.open(str(cursor_file), os.O_CREAT | os.O_RDWR)
        try:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass  # non-POSIX / flock unavailable — degrade to atomic replace

            # Read current cursor state (under lock)
            prev_epoch = 0
            prev_seq = 0
            if cursor_file.exists():
                try:
                    data = json.loads(cursor_file.read_bytes())
                    prev_epoch = data.get('epoch_ms', 0)
                    prev_seq = data.get('seq', 0)
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    pass

            # Advance: if same epoch, increment seq; otherwise reset to 0
            if now_ms == prev_epoch:
                new_seq = prev_seq + 1
            else:
                new_seq = 0

            # Atomic replace with fsync
            new_data = {'epoch_ms': now_ms, 'seq': new_seq}
            tmp = sd / ".tmp-stream-cursor"
            with open(tmp, "w") as f:
                f.write(json.dumps(new_data, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            _chmod_0600(tmp)  # P1-6: 流游标 0600
            os.replace(str(tmp), str(cursor_file))
            _chmod_0600(cursor_file)  # P1-6
            self._fsync_dir(sd)  # P2-4
        finally:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            os.close(lock_fd)

        return f"{now_ms}/{new_seq:0{SEQ_WIDTH}d}"

    def read_cursor(self, session_id: str) -> str:
        """Read the current opaque stream cursor for a session.

        Returns the cursor string ``"epoch_ms/seq"``, or
        :data:`~codeagent.constants.STREAM_CURSOR_INITIAL` if no cursor
        file exists yet.
        """
        sd = self.session_dir(session_id)
        cursor_file = sd / STREAM_CURSOR_FILE

        if not cursor_file.exists():
            return STREAM_CURSOR_INITIAL

        try:
            data = json.loads(cursor_file.read_bytes())
            epoch_ms = data.get('epoch_ms', 0)
            seq = data.get('seq', 0)
            return f"{epoch_ms}/{seq:0{SEQ_WIDTH}d}"
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return STREAM_CURSOR_INITIAL

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
        _mkdir_0700(hd)  # P1-6: history 目录 0700
        dest = hd / f"{message['msg_id']}.json"
        if dest.exists():
            raise ValueError(f"history entry already exists: {message['msg_id']}")
        tmp = hd / f".tmp-{message['msg_id']}.json"
        try:
            with open(tmp, "x") as f:  # O_EXCL: concurrent duplicate appends fail
                f.write(json.dumps(message, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            _chmod_0600(tmp)  # P1-6: history 消息体 0600
        except FileExistsError:
            raise ValueError(f"history entry already exists: {message['msg_id']}")
        os.replace(str(tmp), str(dest))
        _chmod_0600(dest)  # P1-6
        self._fsync_dir(hd)  # P2-4
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
                    # P1-1: 关联键透出 —— 插件以 command_id（= request_id）
                    # 回传 runtime.command_ack 命中命令表主键。
                    "command_id": msg.get("command_id", ""),
                })
            except (json.JSONDecodeError, UnicodeDecodeError):
                summaries.append({"from": "?", "kind": "?", "subject": "(unreadable)", "msg_id": f.stem})
        return {"pending": len(files), "messages": summaries}

    # ── Read (two-phase consumption) ───────────────────────────────────

    def read(
        self, session_id: str, agent_id: str, owner: str,
        skip_msg_ids: Optional[set[str]] = None,
        target_msg_id: Optional[str] = None,  # P1-8: claim a specific message
    ) -> Optional[dict]:
        """Read a message (inbox→processing). Returns message dict or None.

        ``skip_msg_ids``: message ids (file stems) to skip — parked messages
        that must not block the queue head (P1-1 ack-route-unresolved).
        ``target_msg_id``: when set, claim this specific message instead of
        the oldest (P1-8: precise claim to avoid claim-drift).
        """
        skip_msg_ids = skip_msg_ids or set()
        inbox = self.agent_subdir(session_id, agent_id, "inbox")
        processing = self.agent_subdir(session_id, agent_id, "processing")
        corrupt_dir = self.agent_subdir(session_id, agent_id, "_corrupt")
        # P2-7: message ids whose claim is already held by another claimant
        # (same-owner re-claim racing). Never retried in this call — the
        # winner is actively handling them — but other candidates still flow.
        contested: set[str] = set()

        while True:
            files = self.list_messages(inbox)
            if not files:
                return None

            # P1-8: when target_msg_id is set, claim that specific file;
            # otherwise fall back to oldest non-parked (P1-1).
            if target_msg_id:
                target = next((f for f in files if f.stem == target_msg_id), None)
                if target is None:
                    return None  # P1-8: target already consumed or missing
            else:
                target = next(
                    (f for f in files
                     if f.stem not in skip_msg_ids and f.stem not in contested),
                    None,
                )
                if target is None:
                    return None
            try:
                msg = json.loads(target.read_bytes())
            except FileNotFoundError:
                continue  # P2-7: another reader claimed it between list and read
            except (json.JSONDecodeError, UnicodeDecodeError):
                _mkdir_0700(corrupt_dir)  # P1-6: _corrupt 目录 0700
                try:
                    os.replace(str(target), str(corrupt_dir / target.name))
                except OSError:
                    continue  # vanished while being handled — re-scan
                continue

            # Full validation: session + recipient + filename
            ok, reason = validate_message(msg, session_id, agent_id, target.name)
            if not ok:
                _mkdir_0700(corrupt_dir)  # P1-6: _corrupt 目录 0700
                os.replace(str(target), str(corrupt_dir / target.name))
                continue

            # P2-17: reject messages whose sender is not in the authoritative
            # session roster (manager ∪ agents) — the same authority
            # kernel.ingest uses. A forged message (spoofed ``from``) could
            # otherwise be claimed here and, when it demands an ack, drive a
            # forged READ receipt back at the impersonated sender. Roster
            # members always pass; non-members are quarantined like other
            # invalid messages so one forged file cannot block the queue head.
            meta = self.read_session(session_id)
            roster = {meta.get("manager", "")} | set(meta.get("agents", [])) if meta else set()
            if msg.get("from", "") not in roster:
                log.warning(
                    "read: quarantining message %s from non-roster sender %r "
                    "(session %s, agent %s)",
                    target.name, msg.get("from", ""), session_id, agent_id,
                )
                _mkdir_0700(corrupt_dir)  # P1-6: _corrupt 目录 0700
                os.replace(str(target), str(corrupt_dir / target.name))
                continue

            _mkdir_0700(processing)  # P1-6: processing 目录 0700
            dest = processing / target.name
            # Unique tmp claim to avoid concurrent collision
            import random as _rand
            nonce = _rand.randint(1000, 9999)
            claim_file = processing / f".claim-{target.stem}-{owner}.json"
            tmp_claim = processing / f".tmp-claim-{target.stem}-{owner}-{nonce}.json"

            claim_meta = {
                "owner": owner,
                "claimed_at": datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
                "msg_id": target.stem,
            }
            try:
                with open(tmp_claim, "x") as fc:  # O_EXCL: fail if exists
                    fc.write(json.dumps(claim_meta))
                    fc.flush()
                    os.fsync(fc.fileno())
                _chmod_0600(tmp_claim)  # P1-6: claim 0600（hard link 后 claim 文件同权限）
            except FileExistsError:
                tmp_claim.unlink(missing_ok=True)
                continue

            try:
                # P2-7: publish the claim WITHOUT overwriting an existing
                # one. os.replace() clobbered a peer's claim file (A9
                # regression): a second claimant could overwrite the first
                # owner's claim and then delete it on its own failed message
                # move, leaving the claimed message orphaned in processing/.
                # os.link fails with FileExistsError when the claim already
                # exists — an atomic no-clobber publish (tmp is fully
                # written + fsynced before the link, so the claim never
                # appears partially written).
                os.link(str(tmp_claim), str(claim_file))
                # P1-1: the tmp claim copy is unlinked only AFTER the message
                # move (below) — keeping the crash window between claim
                # publish and message move as short as possible. A crash in
                # between leaves a fully written claim + a message still in
                # inbox, which recover_stale()/read() reap as an orphan claim.
            except FileExistsError:
                # The claim already exists — either a peer is actively
                # handling this message, or the previous claimant crashed.
                # P1-1: when the existing claim's lease has expired, reap it
                # (its owner is gone) and retry claiming this message; a
                # fresh claim is genuinely contested — skip it.
                tmp_claim.unlink(missing_ok=True)
                if self._reap_expired_claim(claim_file, inbox, processing):
                    continue  # claim removed — re-scan and retry
                contested.add(target.stem)
                continue
            except OSError:
                # Filesystem without hard-link support: fall back to a
                # direct no-clobber O_EXCL write of the final claim file.
                tmp_claim.unlink(missing_ok=True)
                try:
                    with open(claim_file, "x") as fc:
                        fc.write(json.dumps(claim_meta))
                        fc.flush()
                        os.fsync(fc.fileno())
                    _chmod_0600(claim_file)  # P1-6: claim 0600
                except FileExistsError:
                    tmp_claim.unlink(missing_ok=True)
                    # P1-1: same expired-claim reap as the os.link path above.
                    if self._reap_expired_claim(claim_file, inbox, processing):
                        continue  # claim removed — re-scan and retry
                    contested.add(target.stem)
                    continue

            try:
                os.replace(str(target), str(dest))
            except OSError:
                tmp_claim.unlink(missing_ok=True)
                claim_file.unlink(missing_ok=True)
                continue

            # P1-1: the move succeeded — only now drop the tmp claim copy.
            tmp_claim.unlink(missing_ok=True)
            # P2-4: make the inbox → processing rename durable before the
            # claim is considered authoritative.
            self._fsync_dir(inbox)
            self._fsync_dir(processing)

            return msg

    def _reap_expired_claim(self, claim_file: Path, inbox: Path, processing: Path) -> bool:
        """P1-1: reap a claim whose lease expired so read() can retry.

        Called when a claim file blocks our own claim (contested). When the
        existing claim is older than ``LEASE_TIMEOUT_S`` the previous
        claimant crashed: the claim is deleted and — if the message already
        reached processing/ — moved back to inbox. Returns True when the
        claim was removed (caller retries); False when the claim is fresh
        (genuinely contested) or unreadable (P2-1).

        P2-7: adds ``LEASE_CLOCK_TOLERANCE_S`` to the cutoff so that
        cross-device wall-clock skew (up to the tolerance) does not cause
        a fresh claim to be falsely reaped.  Operators MUST ensure NTP is
        configured on every swarm host.
        """
        now = datetime.now(timezone.utc).timestamp()
        # P2-7: extend cutoff by clock tolerance for cross-device skew
        cutoff = now - LEASE_TIMEOUT_S + LEASE_CLOCK_TOLERANCE_S
        try:
            claim = json.loads(claim_file.read_bytes())
            claimed_at_s = claim.get("claimed_at", "")
            if not claimed_at_s:
                return False  # P2-1: no timestamp — treat as contested
            claimed_ts = datetime.fromisoformat(claimed_at_s).timestamp()
        except (json.JSONDecodeError, UnicodeDecodeError,
                ValueError, TypeError, OSError):  # P2-1
            return False
        if claimed_ts >= cutoff:
            return False  # fresh claim — owner is actively working
        msg_id = claim.get("msg_id")
        if not msg_id:
            # P3-4: regex fallback is ambiguous when owner contains dashes
            # (.claim-foo-bar-alice-smith: is owner "alice-smith" or "smith"?).
            # Use the owner from JSON to strip the known suffix reliably.
            owner_raw = claim.get("owner", "")
            prefix = ".claim-"
            suffix = f"-{owner_raw}" if owner_raw else ""
            stem = claim_file.stem
            if stem.startswith(prefix) and suffix and stem.endswith(suffix):
                msg_id = stem[len(prefix):-len(suffix)]
            else:
                msg_id = stem
        owner = claim.get("owner", "")
        # P2-2: same per-claim flock as renew_claim()/recover_stale() — never
        # reap a claim that a running task just renewed.
        with self._claim_lock(processing, msg_id, owner):
            try:
                cur = json.loads(claim_file.read_bytes())
                cur_ts = datetime.fromisoformat(cur.get("claimed_at", "")).timestamp()
            except (json.JSONDecodeError, UnicodeDecodeError,
                    ValueError, TypeError, OSError):  # P2-1
                cur_ts = claimed_ts
            if cur_ts >= cutoff:
                return False  # renewed while we waited — still active
            msg_file = processing / f"{msg_id}.json"
            if msg_file.exists():
                os.replace(str(msg_file), str(inbox / msg_file.name))
                self._fsync_dir(inbox)  # P2-4
            claim_file.unlink(missing_ok=True)
            self._fsync_dir(processing)  # P2-4
            return True

    # ── Finalize ───────────────────────────────────────────────────────

    @staticmethod
    def _validate_msg_id(msg_id: str) -> None:
        # P2-3: strict whitelist (same charset as agent ids, 64-char cap).
        # The old path-separator check let ".tmp-"-prefixed ids, "..", and
        # glob metacharacters ("[", "*") through — those could break file
        # layout assumptions or broaden claim/finalize glob patterns.
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", msg_id):
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

        _mkdir_0700(archive)  # P1-6: archive 目录 0700
        os.replace(str(target), str(archive / target.name))
        claim_file.unlink(missing_ok=True)
        # P2-4: processing → archive rename durable before returning.
        self._fsync_dir(processing)
        self._fsync_dir(archive)
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

        # If another consumer holds an active claim on a message in
        # processing/, it owns the finalize — skip to avoid double-archive
        # races. Same-owner claims (the receiver itself two-phase-read it)
        # are fine to finalize.
        if src.parent == processing:
            claims = sorted(processing.glob(f".claim-{msg_id}-*.json"))
            # P2-16: compare the claim OWNER exactly, parsed from the claim
            # file. The old substring test (`owner not in c.name`) let owner
            # "alice" finalize a message claimed by "alice2" ("alice" is a
            # substring of "alice2"). Unreadable claims count as foreign.
            foreign = []
            for c in claims:
                try:
                    cowner = json.loads(c.read_bytes()).get("owner", "")
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    cowner = ""
                if cowner != owner:
                    foreign.append(c)
            if foreign:
                raise ValueError(
                    f"msg {msg_id} has an active foreign claim; use finalize() instead"
                )

        _mkdir_0700(archive)  # P1-6: archive 目录 0700
        os.replace(str(src), str(archive / src.name))
        # P2-4: inbox/processing → archive rename durable before returning.
        self._fsync_dir(src.parent)
        self._fsync_dir(archive)
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
        # P2-4: processing → inbox rename durable before returning.
        self._fsync_dir(processing)
        self._fsync_dir(inbox)
        return f"released → inbox/{target.name}"

    # ── Recover stale ──────────────────────────────────────────────────

    def renew_claim(self, session_id: str, agent_id: str, msg_id: str, owner: str) -> bool:
        """P2-10: refresh a claim's lease so a task still running is not
        recycled as stale.

        ``recover_stale()`` treats a claim as stale when its ``claimed_at``
        is older than ``LEASE_TIMEOUT_S`` — so a task running longer than
        5 minutes would be re-dispatched (duplicate execution). Long-running
        workers call this periodically while the task runs: it bumps
        ``claimed_at`` (and records ``renewed_at``) so the claim stays fresh.

        Returns ``True`` when renewed; ``False`` when the claim is missing,
        owned by someone else, or the message is no longer in processing/
        (already finalized/recovered).
        """
        self._validate_msg_id(msg_id)
        processing = self.agent_subdir(session_id, agent_id, "processing")
        target = processing / f"{msg_id}.json"
        if not target.exists():
            return False  # message finalized/recovered — nothing to renew
        claim_files = sorted(processing.glob(f".claim-{msg_id}-*.json"))
        if len(claim_files) != 1:
            return False
        claim_file = claim_files[0]
        # P2-2: same per-claim flock as recover_stale()/read() reap — renew
        # must not interleave with a recover that is reaping this claim, or
        # the freshly renewed claim would be moved back to inbox behind us.
        with self._claim_lock(processing, msg_id, owner):
            # Re-check under the lock: recover_stale() may have removed the
            # claim (and moved the message) while we waited.
            if not target.exists():
                return False
            claim_files = sorted(processing.glob(f".claim-{msg_id}-*.json"))
            if len(claim_files) != 1:
                return False
            claim_file = claim_files[0]
            try:
                claim = json.loads(claim_file.read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError,
                    ValueError, TypeError, OSError):
                return False
            if claim.get("owner") != owner:
                return False
            now = datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT)
            claim["claimed_at"] = now
            claim["renewed_at"] = now
            tmp = processing / f".tmp-renew-{msg_id}-{owner}.json"
            try:
                with open(tmp, "w") as f:
                    f.write(json.dumps(claim, indent=2, ensure_ascii=False))
                    f.flush()
                    os.fsync(f.fileno())
                _chmod_0600(tmp)  # P1-6: claim 0600
                os.replace(str(tmp), str(claim_file))
                _chmod_0600(claim_file)  # P1-6
                self._fsync_dir(processing)  # P2-4
            except OSError:
                tmp.unlink(missing_ok=True)
                return False
            # Self-heal a finalize/recover race: if the message left
            # processing/ between the check and the replace, remove the
            # resurrected claim.
            if not target.exists():
                claim_file.unlink(missing_ok=True)
                return False
            return True

    def recover_stale(self, session_id: str, agent_id: str) -> str:
        processing_dir = self.agent_subdir(session_id, agent_id, "processing")
        inbox = self.agent_subdir(session_id, agent_id, "inbox")
        if not processing_dir.exists():
            return "no processing/ directory"

        recovered = 0
        # P2-10: the lease basis is the claim's claimed_at, which renew_claim()
        # refreshes — an actively renewed claim (task still running) is never
        # older than the cutoff, so it is not recycled.
        # P2-7: extend cutoff by clock tolerance for cross-device skew.
        # NTP MUST be configured on every swarm host.
        cutoff = datetime.now(timezone.utc).timestamp() - LEASE_TIMEOUT_S + LEASE_CLOCK_TOLERANCE_S
        # P1-1: orphan-claim age gate — a claim whose message is missing from
        # processing/ is only reaped once it is older than this, so a
        # claimant that is between os.link (claim publish) and os.replace
        # (message move) is never disturbed (that window is microseconds, and
        # read()'s P1-1 reorder removed every interruptible step from it).
        orphan_age_gate = datetime.now(timezone.utc).timestamp() - 30.0
        for cf in sorted(processing_dir.glob(".claim-*.json")):
            try:
                claim = json.loads(cf.read_bytes())
                claimed_at_s = claim.get("claimed_at", "")
                if not claimed_at_s:
                    continue  # P2-1: missing timestamp — leave untouched
                claimed_ts = datetime.fromisoformat(claimed_at_s).timestamp()
                # P3-i: extract msg_id robustly — claim stem is
                # .claim-{msg_id}-{owner} where msg_id may contain dashes.
                # Prefer the JSON field; fall back to a regex that strips the
                # leading ".claim-" prefix and the trailing "-{owner}" suffix.
                msg_id = claim.get("msg_id")
                if not msg_id:
                    # P3-4: regex fallback is ambiguous when owner contains
                    # dashes (.claim-foo-bar-alice-smith: is owner "alice-smith"
                    # or "smith"?). Use the owner from JSON to strip the known
                    # suffix reliably.
                    owner_for_parse = claim.get("owner", "")
                    prefix = ".claim-"
                    suffix = f"-{owner_for_parse}" if owner_for_parse else ""
                    if cf.stem.startswith(prefix) and suffix and cf.stem.endswith(suffix):
                        msg_id = cf.stem[len(prefix):-len(suffix)]
                    else:
                        msg_id = cf.stem
                owner = claim.get("owner", "")
                msg_file = processing_dir / f"{msg_id}.json"
                # P2-2: hold the per-claim flock across the whole
                # read-decide-move — renew_claim() takes the same lock, so a
                # lease-boundary TOCTOU (recover reaps a claim that a running
                # task just renewed) cannot recycle active work.
                with self._claim_lock(processing_dir, msg_id, owner):
                    # Re-read under the lock: a racing renew_claim() that won
                    # has bumped claimed_at — skip claims refreshed meanwhile.
                    try:
                        cur = json.loads(cf.read_bytes())
                        cur_ts = datetime.fromisoformat(cur.get("claimed_at", "")).timestamp()
                    except (json.JSONDecodeError, UnicodeDecodeError,
                            ValueError, TypeError, OSError):  # P2-1
                        cur_ts = claimed_ts
                    if not msg_file.exists():
                        # P1-1: orphan claim — claim exists but no message in
                        # processing/. Either the claimant crashed between
                        # os.link (claim publish) and os.replace (message
                        # move) — the message is still in inbox, re-readable —
                        # or it was already finalized/recovered. Both cases:
                        # drop the claim so the message is not stranded. Not
                        # counted as "recovered": no message was moved.
                        if cur_ts >= orphan_age_gate:
                            continue  # too fresh — possibly mid-move
                        cf.unlink()
                        self._fsync_dir(processing_dir)  # P2-4
                        continue
                    if cur_ts >= cutoff:
                        continue  # active (possibly renewed) — leave it
                    os.replace(str(msg_file), str(inbox / msg_file.name))
                    cf.unlink()
                    # P2-4: processing → inbox rename durable before returning.
                    self._fsync_dir(processing_dir)
                    self._fsync_dir(inbox)
                    recovered += 1
            except (json.JSONDecodeError, UnicodeDecodeError,
                    ValueError, TypeError, OSError):  # P2-1
                pass

        # P3-1: sweep orphaned tmp-claim files. read() writes
        # .tmp-claim-{msg_id}-{owner}-{nonce}.json BEFORE publishing the
        # claim via os.link, then unlinks it after the message move. A crash
        # between write and link (or between link and unlink) leaves the tmp
        # file behind forever — the main claim loop above only sees
        # ".claim-*.json", so these orphans were never cleaned. A tmp-claim
        # older than the orphan age gate is never part of a live handshake
        # (that window is microseconds), so it is safe to drop.
        for tc in sorted(processing_dir.glob(".tmp-claim-*.json")):
            try:
                if tc.is_symlink():
                    tc.unlink(missing_ok=True)
                    continue
                if tc.stat().st_mtime >= orphan_age_gate:
                    continue  # possibly mid-write — leave it
                tc.unlink(missing_ok=True)
            except OSError:
                continue  # vanished while sweeping — fine
        self._fsync_dir(processing_dir)  # P2-4
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
            updated_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
        )
        dest = ad / "status.json"
        # P2-5: unique tmp name per writer (pid + uuid, aligned with
        # _persist_meta's -{pid}-{uuid} pattern) — concurrent write_status()
        # calls previously shared .tmp-status.json and could publish each
        # other's partial writes, corrupting status.json.
        tmp = ad / f".tmp-status-{os.getpid()}-{uuid4().hex[:8]}.json"
        with open(tmp, "w") as f:
            f.write(json.dumps(status.to_dict(), indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        _chmod_0600(tmp)  # P1-6: status.json 0600（含 current_task/last_conclusion 等敏感内容）
        os.replace(str(tmp), str(dest))
        _chmod_0600(dest)  # P1-6
        self._fsync_dir(ad)  # P2-4
        return f"status: {state}"

    def read_status(self, session_id: str, agent_id: str, *, strict: bool = False) -> Optional[StatusSnapshot]:
        """Read status.json; ``None`` when absent or invalid.

        P2-5: with ``strict=True`` a present-but-corrupt status file raises
        :class:`StatusFileCorruptError` — a structured error carrying the
        path and reason — instead of collapsing into ``None``, so callers can
        distinguish "no status" from "status lost to a crash". The default
        (non-strict) keeps the historical ``None`` contract for existing
        callers/tests.
        """
        status_file = self.agent_dir(session_id, agent_id) / "status.json"
        try:
            if not status_file.exists():
                return None
            d = json.loads(status_file.read_bytes())
            # Strict validation: exactly 5 required keys, all strings
            required = {"session_id", "state", "current_task", "last_conclusion", "updated_at"}
            if not isinstance(d, dict) or set(d.keys()) != required:
                if strict:
                    raise StatusFileCorruptError(status_file, "missing or extra keys")
                return None
            if not all(isinstance(d[k], str) for k in required):
                if strict:
                    raise StatusFileCorruptError(status_file, "non-string field value")
                return None
            if d["state"] not in VALID_STATES:
                if strict:
                    raise StatusFileCorruptError(status_file, f"invalid state {d['state']!r}")
                return None
            if d["session_id"] != session_id:
                if strict:
                    raise StatusFileCorruptError(status_file, f"session mismatch: {d['session_id']!r}")
                return None
            return StatusSnapshot.from_dict(d)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # P2-5: unparseable content — the concurrent-write corruption
            # signature. Structured error in strict mode; None otherwise.
            if strict:
                raise StatusFileCorruptError(status_file, str(exc)) from exc
            return None
        except OSError:
            return None

    # ── Stats / Clear ──────────────────────────────────────────────────

    def stats(self, session_id: str, agent_id: str) -> dict[str, int]:
        ad = self.agent_dir(session_id, agent_id)
        return {d: len(self.list_messages(ad / d)) for d in ("inbox", "processing", "archive", "_corrupt")}

    def clear(self, session_id: str, agent_id: str, *, prune_stale: bool = False) -> str:
        """Clear archive only. Use purge() for _corrupt."""
        self._check_park_lease(session_id, agent_id)
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

    # ── A6: whole-session retention cleanup ────────────────────────────

    def _session_has_active_park_lease(self, session_id: str) -> bool:
        """True when any roster member of *session_id* holds a HOT_PARKED lease."""
        if ParkRegistry is None:
            return False
        try:
            from codeagent.domain.park import Lifecycle

            registry = ParkRegistry()
            meta = self.read_session(session_id) or {}
            agents = set(meta.get("agents", [])) | {meta.get("manager", "")}
            for aid in agents:
                if not aid:
                    continue
                manifest = registry.lookup_by_field("mailbox_agent_id", aid)
                if manifest is not None and manifest.lifecycle == Lifecycle.HOT_PARKED:
                    return True
        except Exception:
            # Park registry unavailable or corrupt — never block cleanup on it.
            return False
        return False

    def _session_has_unfinished_work(self, session_id: str) -> bool:
        """P2-6: True when any roster member still has pending messages or an
        active claim lease.

        ``clean_older_than()`` must never delete an actively used session just
        because its ``created_at`` is old: pending inbox messages (not yet
        claimed), in-flight processing/ messages (claimed but not finalized),
        a fresh claim lease, or queued outbox entries all mean the session is
        live and must be skipped.
        """
        meta = self.read_session(session_id) or {}
        agents = set(meta.get("agents", [])) | {meta.get("manager", "")}
        for aid in agents:
            if not aid:
                continue
            ad = self.agent_dir(session_id, aid)
            inbox = ad / "inbox"
            if inbox.is_dir() and any(
                # P3-6: symlinks must not count as pending work
                f.is_file() and not f.is_symlink() and f.suffix == ".json" and not f.name.startswith(".")
                for f in inbox.iterdir()
            ):
                return True  # pending, not yet claimed
            proc = ad / "processing"
            if proc.is_dir():
                if any(
                    # P3-6: symlinks must not count as in-flight work
                    f.is_file() and not f.is_symlink() and f.suffix == ".json" and not f.name.startswith(".")
                    for f in proc.iterdir()
                ):
                    return True  # in-flight, claimed but not finalized
                # A fresh claim lease = active claim (message may have been
                # moved already; the lease itself marks liveness).
                now = datetime.now(timezone.utc).timestamp()
                # P2-7: extend cutoff by clock tolerance for cross-device skew
                claim_cutoff = now - LEASE_TIMEOUT_S + LEASE_CLOCK_TOLERANCE_S
                for cf in proc.glob(".claim-*.json"):
                    if cf.is_symlink():
                        continue  # P3-6: never treat a symlink as a lease
                    try:
                        claim = json.loads(cf.read_bytes())
                        claimed_ts = datetime.fromisoformat(
                            claim.get("claimed_at", "")
                        ).timestamp()
                    except (json.JSONDecodeError, UnicodeDecodeError,
                            ValueError, TypeError, OSError):
                        continue
                    if claimed_ts >= claim_cutoff:
                        return True
        # Queued cross-host outbox entries are unfinished deliveries.
        outbox = self.root / "_outbox" / session_id
        if outbox.is_dir() and any(
            e.is_file() and not e.is_symlink()  # P3-6: symlink entries are not deliveries
            for e in outbox.iterdir()
        ):
            return True
        return False

    def clean_older_than(self, days: int) -> dict:
        """A6: delete whole sessions older than *days*.

        A session is eligible when its ``session.json`` ``created_at`` is
        older than the cutoff.  For each eligible session the ENTIRE
        session dir (history/archive/events/per-agent inbox/processing/
        status) is removed, plus the session's ``_outbox/<sid>`` and
        ``_dead_letter/<sid>`` dirs under this store root.  Sessions with
        an active park lease (HOT_PARKED) are skipped.

        Returns ``{"removed": [session_id, ...], "skipped": [session_id, ...]}``.
        """
        import shutil

        if days < 0:
            raise ValueError("days must be non-negative")
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        removed: list[str] = []
        skipped: list[str] = []
        if not self.root.is_dir():
            return {"removed": removed, "skipped": skipped}
        for sd in sorted(self.root.iterdir()):
            if not sd.is_dir() or sd.name.startswith(".") or sd.name.startswith("_"):
                continue  # skip dot/private dirs (_outbox/_dead_letter/…)
            sid = sd.name
            try:
                meta = self.read_session(sid)
            except ValueError:
                continue  # not a session dir (invalid id shape)
            created_at = (meta or {}).get("created_at", "")
            try:
                ts = datetime.fromisoformat(created_at).timestamp() if created_at else time.time()
            except ValueError:
                ts = time.time()
            if ts >= cutoff:
                continue
            # P2-6: created_at alone must not authorize deletion — an old
            # session with pending messages / an active claim lease is live.
            if self._session_has_unfinished_work(sid):
                skipped.append(sid)
                continue
            # Park lease guard: never delete a session a hot runtime needs.
            if self._session_has_active_park_lease(sid):
                skipped.append(sid)
                continue
            try:
                shutil.rmtree(str(sd), ignore_errors=True)
                shutil.rmtree(str(self.root / "_outbox" / sid), ignore_errors=True)
                shutil.rmtree(str(self.root / "_dead_letter" / sid), ignore_errors=True)
                removed.append(sid)
            except OSError:
                skipped.append(sid)
        return {"removed": removed, "skipped": skipped}

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
                _mkdir_0700(corrupt_dir)  # P1-6: _corrupt 目录 0700
                os.replace(str(entry), str(corrupt_dir / filename))
                continue

            _mkdir_0700(archive)  # P1-6: archive 目录 0700
            os.replace(str(entry), str(archive / filename))
            results.append(msg)

        return results


# ── Request lifecycle ledger ────────────────────────────────────────────

TERMINAL_STATES = frozenset({"DONE", "BLOCKED", "CANCELLED", "EXPIRED", "UNKNOWN_STALE"})
_NON_TERMINAL = frozenset({"DISPATCHED", "ACKED", "RUNNING"})


class RequestLedger:
    """Append-only per-request event ledger with terminal CAS.

    Each ``(request_id, run_id)`` pair accumulates an event log in
    ``<session_dir>/<agent_id>/events/<request_id>/events.jsonl``.
    Exactly one terminal state (DONE, BLOCKED, CANCELLED, EXPIRED) may be
    recorded — subsequent terminal writes are rejected (PROTOCOL_CONFLICT).
    Non-terminal events are always appended.
    """

    def __init__(self, session_dir: Path, agent_id: str) -> None:
        self._session_dir = session_dir
        self._agent_id = agent_id

    # -- internal paths ---------------------------------------------------

    def _events_dir(self, request_id: str) -> Path:
        """Directory holding the JSONL event log for *request_id*.

        P1-2 defense-in-depth: *request_id* becomes a directory name —
        re-validate it here even though ``validate_message`` covers inbox
        messages, because the ledger is also driven directly by gateway
        API params (artifact.verify) and other callers.
        """
        validate_path_component(request_id, "request_id")
        return self._session_dir / self._agent_id / "events" / request_id

    def _events_file(self, request_id: str) -> Path:
        return self._events_dir(request_id) / "events.jsonl"

    # -- public API -------------------------------------------------------

    def record_event(
        self, request_id: str, run_id: str, event: str, meta: dict
    ) -> bool:
        """Append *event* to the ledger for *(request_id, run_id)*.

        Returns ``True`` when the event was appended.
        Returns ``False`` (PROTOCOL_CONFLICT) when *event* is a terminal
        state and a terminal has already been recorded for this pair.
        Non-terminal events are **always** accepted.

        Uses ``fcntl.flock`` to make the check-then-append atomic across
        concurrent processes writing to the same request_id directory.
        """
        events_dir = self._events_dir(request_id)
        _mkdir_0700(events_dir)  # P1-6: events 目录 0700
        lock_path = events_dir / ".lock"
        lock_fd = open(lock_path, "w")
        _chmod_0600(lock_path)  # P1-6: 锁文件 0600
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if event in TERMINAL_STATES:
                existing = self.get_terminal(request_id, run_id)
                if existing is not None:
                    self._append(request_id, run_id, "PROTOCOL_CONFLICT",
                                 {"frozen_event": event, "existing_terminal": existing})
                    return False

            self._append(request_id, run_id, event, meta)
            return True
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    def get_terminal(self, request_id: str, run_id: str) -> str | None:
        """Return the first terminal state recorded, or ``None``."""
        for entry in self._read_entries(request_id, run_id):
            if entry["event"] in TERMINAL_STATES:
                return entry["event"]
        return None

    def apply_message(self, msg: dict) -> str:
        """Reduce one mailbox message onto the request ledger.

        Maps a message to the canonical request lifecycle state and
        records it (idempotently, terminal CAS enforced):

          TASK/INIT        → DISPATCHED   (first TASK for (request_id, run_id))
          RECEIPT(READ)    → ACKED        (reply_to = acked msg_id)
          PROGRESS (first) → RUNNING
          REPORT           → DONE         (terminal — the task's final outcome)
          NOTICE           → informational (never terminal)
          anything else    → no-op

        Returns the recorded event name, or "" when the message maps to
        no state transition. The ledger stays the single source of truth
        for request state; ``status.json`` only reflects availability.
        """
        kind = msg.get("kind", "")
        run_id = msg.get("run_id", "") or ""
        request_id = msg.get("request_id", "") or ""
        if not request_id or not run_id:
            return ""

        if kind == "RECEIPT":
            if msg.get("receipt_type") != "READ":
                return ""
            self.record_event(request_id, run_id, "ACKED", {
                "msg_id": msg.get("msg_id", ""),
                "reply_to": msg.get("reply_to", ""),
            })
            return "ACKED"

        if kind in ("TASK", "INIT"):
            self.record_event(request_id, run_id, "DISPATCHED", {
                "msg_id": msg.get("msg_id", ""),
                "from": msg.get("from", ""),
            })
            return "DISPATCHED"

        if kind == "PROGRESS":
            # Only the FIRST correlated PROGRESS transitions to RUNNING;
            # later progress events are informational.
            if self.get_terminal(request_id, run_id) is None:
                has_running = any(
                    e["event"] == "RUNNING"
                    for e in self._read_entries(request_id, run_id)
                )
                if not has_running:
                    self.record_event(request_id, run_id, "RUNNING", {
                        "msg_id": msg.get("msg_id", ""),
                    })
                    return "RUNNING"
            return ""

        if kind == "REPORT":
            # P2-11: a REPORT is the task's final outcome — map it to the
            # terminal DONE state so find_stale() never keeps flagging a
            # finished request as "ACKED but never terminal" (the old code
            # recorded a non-terminal "REPORT" event and the watchdog kept
            # misreporting completed work as stale forever).
            self.record_event(request_id, run_id, "DONE", {
                "msg_id": msg.get("msg_id", ""),
                "from": msg.get("from", ""),
                "source": "report",
            })
            return "DONE"

        if kind == "NOTICE":
            # Informational only — never terminal.
            self.record_event(request_id, run_id, "NOTICE", {
                "msg_id": msg.get("msg_id", ""),
                "from": msg.get("from", ""),
            })
            return "NOTICE"

        return ""

    def get_events(self, request_id: str, run_id: str) -> list[dict]:
        """Return all events for *(request_id, run_id)*, newest last."""
        return self._read_entries(request_id, run_id)

    def get_entries_all_runs(self, request_id: str) -> dict[str, list[dict]]:
        """Return all entries for *request_id*, grouped by run_id.

        Public API over ``_read_entries_all_runs`` so callers (e.g.
        ``oracle status``) never reach into the private implementation
        (I4).
        """
        return self._read_entries_all_runs(request_id)

    def record_artifact_verdict(
        self, request_id: str, run_id: str, verified: bool
    ) -> dict:
        """Record an artifact verification outcome as a terminal event.

        *verified=True*  → terminal ``DONE``  (meta ``{"source":"artifact_verify"}``).
        *verified=False* → terminal ``BLOCKED`` (meta ``{"source":"artifact_verify",
        "reason":"sha256_mismatch"}``).

        Returns ``{"terminal": <state>, "cas": True}`` when the terminal was
        newly recorded, or ``{"terminal": <existing>, "cas": False}`` when a
        prior terminal already existed (terminal CAS conflict).
        """
        if verified:
            state = "DONE"
            meta: dict = {"source": "artifact_verify"}
        else:
            state = "BLOCKED"
            meta = {"source": "artifact_verify", "reason": "sha256_mismatch"}
        accepted = self.record_event(request_id, run_id, state, meta)
        if accepted:
            return {"terminal": state, "cas": True}
        # Terminal CAS conflict — a prior terminal already exists.
        existing = self.get_terminal(request_id, run_id)
        return {"terminal": existing or state, "cas": False}

    def find_stale(self, sla_seconds: int = 300) -> list[dict]:
        """Return ACKED-but-not-terminal requests older than *sla_seconds*.

        Each result dict contains at minimum ``request_id``, ``run_id``,
        and ``acked_ts`` (the UNIX timestamp of the ACKED event).
        """
        now = time.time()
        results: list[dict] = []
        events_root = self._session_dir / self._agent_id / "events"
        if not events_root.is_dir():
            return results

        for req_dir in sorted(events_root.iterdir()):
            if not req_dir.is_dir():
                continue
            request_id = req_dir.name
            entries = self._read_entries_all_runs(request_id)
            for run_id, events in entries.items():
                has_terminal = any(e["event"] in TERMINAL_STATES for e in events)
                if has_terminal:
                    continue
                for e in events:
                    if e["event"] == "ACKED" and (now - e["ts"]) > sla_seconds:
                        results.append({
                            "request_id": request_id,
                            "run_id": run_id,
                            "acked_ts": e["ts"],
                        })
                        break  # one entry per (request_id, run_id)
        return results

    # -- internals --------------------------------------------------------

    def _append(self, request_id: str, run_id: str, event: str, meta: dict) -> None:
        entry = {
            "request_id": request_id,
            "run_id": run_id,
            "event": event,
            "ts": time.time(),
            "meta": meta,
        }
        d = self._events_dir(request_id)
        _mkdir_0700(d)  # P1-6: events 目录 0700
        with open(self._events_file(request_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # P2-11: fsync the append — without it a crash could lose the
            # just-recorded terminal event and leave find_stale() reporting a
            # request as non-terminal (stale) after the process already
            # considered it finished.
            f.flush()
            os.fsync(f.fileno())
        _chmod_0600(self._events_file(request_id))  # P1-6: events.jsonl 0600

    def _read_entries(self, request_id: str, run_id: str) -> list[dict]:
        """Read entries filtered to a specific run_id."""
        return [
            e for e in self._read_all(request_id)
            if e.get("run_id") == run_id
        ]

    def _read_entries_all_runs(self, request_id: str) -> dict[str, list[dict]]:
        """Group entries by run_id."""
        by_run: dict[str, list[dict]] = {}
        for e in self._read_all(request_id):
            by_run.setdefault(e.get("run_id", ""), []).append(e)
        return by_run

    def _read_all(self, request_id: str) -> list[dict]:
        """Read every line from the JSONL file for *request_id*."""
        p = self._events_file(request_id)
        if not p.exists():
            return []
        entries: list[dict] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        return entries
