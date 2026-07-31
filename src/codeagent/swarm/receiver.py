"""SwarmReceiver — real-time message push for agents.

Connects the transport push channel (SSHStream from B2) with the
kernel callback surface (SwarmKernel.subscribe from C1) so that
agents receive messages via callbacks without polling.

Two modes:
  - **Watch mode** (local): polls a local mailbox inbox directory
    using stat-based mtime detection (stdlib only, no watchdog dep).
  - **Stream mode** (remote): opens an SSHStream to a remote host
    running ``codeagent remote-exec serve`` and processes incoming
    ``MSG_STREAM_EVENT`` frames pushed by the server.

Lifecycle::

    receiver = SwarmReceiver(session_id="s1", agent_id="w1",
                             kernel=kernel, store=store)
    receiver.subscribe(callback=my_handler)
    receiver.start_watch(mailbox_root=Path("/tmp/mbox"), poll_interval=0.5)
    # or: receiver.start_stream(ssh_cmd=["ssh", "host"], cursor="0")
    receiver.loop(timeout=300)
    receiver.stop()
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from codeagent.mailbox.store import MailboxStore
from codeagent.swarm.model import Subscription

if False:  # TYPE_CHECKING
    from codeagent.swarm.kernel import SwarmKernel
    from codeagent.transport.ssh import SSHStream

log = logging.getLogger(__name__)

# Default poll interval for watch mode (seconds).
DEFAULT_WATCH_POLL_INTERVAL = 0.5

# Maximum time to block in loop() per iteration (seconds).
_LOOP_TICK = 0.25


class SwarmReceiver:
    """Per-agent real-time message receiver.

    Connects transport push (stream) or local filesystem watch to the
    SwarmKernel callback surface so agents get messages pushed without
    needing to poll.

    Parameters
    ----------
    session_id : str
        Swarm session this receiver belongs to.
    agent_id : str
        Agent whose inbox is being watched/streamed.
    kernel : SwarmKernel
        Kernel instance for subscribe/ack.
    store : MailboxStore
        Filesystem-backed store for inbox operations and dedup.
    """

    def __init__(
        self,
        session_id: str,
        agent_id: str,
        kernel: SwarmKernel,
        store: MailboxStore,
    ) -> None:
        self._session_id = session_id
        self._agent_id = agent_id
        self._kernel = kernel
        self._store = store

        # Registered callbacks — list of (callback, channels, kinds) tuples.
        self._callbacks: list[tuple[Callable[[dict], None], list[str], list[str]]] = []

        # Transport handles
        self._stream: SSHStream | None = None
        self._watch_thread: threading.Thread | None = None
        self._stream_thread: threading.Thread | None = None

        # Control
        self._stop_event = threading.Event()
        self._started = False

        # Dedup: msg_ids we've already delivered (in-memory + filesystem cross-check).
        self._seen_msg_ids: set[str] = set()

        # Watch mode state
        self._watch_mailbox_root: Path | None = None
        self._watch_poll_interval: float = DEFAULT_WATCH_POLL_INTERVAL
        # Stat cache: filename → (mtime_ns, size) for detecting new/changed files.
        self._stat_cache: dict[str, tuple[int, int]] = {}

    # ── Callback registration ──────────────────────────────────────────

    def subscribe(
        self,
        callback: Callable[[dict], None],
        channels: Optional[list[str]] = None,
        kinds: Optional[list[str]] = None,
    ) -> None:
        """Register a callback for new messages.

        Keeps a local reference so the receiver can fire callbacks
        directly from stream/watch events.  Does NOT call
        ``kernel.subscribe`` — the kernel routes to the receiver
        via ``attach_receiver`` to avoid infinite recursion.
        """
        channels = channels or []
        kinds = kinds or []
        self._callbacks.append((callback, channels, kinds))

    # ── Stream mode (remote push via SSHStream) ────────────────────────

    def start_stream(
        self,
        *,
        ssh_cmd: list[str],
        cursor: str = "0",
        timeout: int = 600,
    ) -> None:
        """Open an SSHStream to the remote host and start receiving events.

        The remote host must be running ``codeagent remote-exec serve``
        with a stream subscription for this session/agent.

        Parameters
        ----------
        ssh_cmd : list[str]
            SSH command prefix, e.g. ``["ssh", "myhost"]``.
        cursor : str
            Initial cursor for resumable delivery.
        timeout : int
            Per-request timeout for the stream.
        """
        if self._started:
            raise RuntimeError("receiver already started")
        self._started = True
        self._stop_event.clear()

        from codeagent.transport.ssh import SSHStream

        self._stream = SSHStream(ssh_cmd=ssh_cmd)
        self._stream_thread = threading.Thread(
            target=self._stream_loop,
            args=(cursor, timeout),
            daemon=True,
            name=f"receiver-stream-{self._agent_id}",
        )
        self._stream_thread.start()

    def _stream_loop(self, cursor: str, timeout: int) -> None:
        """Background thread: poll SSHStream and dispatch events."""
        try:
            self._stream.open(
                session_id=self._session_id,
                agent_id=self._agent_id,
                cursor=cursor,
                timeout=timeout,
            )
        except Exception:
            log.exception("SwarmReceiver: failed to open stream")
            return

        while not self._stop_event.is_set():
            try:
                events = self._stream.poll(timeout=_LOOP_TICK)
            except Exception:
                log.exception("SwarmReceiver: stream poll error")
                break

            for event in events:
                self._handle_stream_event(event)

        # Clean up
        try:
            self._stream.close()
        except Exception:
            pass

    def _handle_stream_event(self, event: dict[str, Any]) -> None:
        """Process a single stream event: write to inbox, fire callbacks, ack."""
        msg_id = event.get("msg_id", "")
        if not msg_id:
            return

        # Dedup
        if msg_id in self._seen_msg_ids:
            return
        self._seen_msg_ids.add(msg_id)

        # Also check filesystem dedup: if the message already exists in
        # inbox or processing or archive, skip writing.
        if self._is_msg_on_disk(msg_id):
            log.debug("SwarmReceiver: skipping already-stored msg_id=%s", msg_id)
            self._fire_callbacks(event)
            # Still ack so the message moves to consumed state
            self._try_ack(msg_id)
            return

        # Write to local inbox
        self._write_to_inbox(event)

        # Fire callbacks
        self._fire_callbacks(event)

        # Auto-ack consumed
        self._try_ack(msg_id)

    def _write_to_inbox(self, event: dict[str, Any]) -> None:
        """Write a stream event as a message file in the local inbox."""
        msg_id = event.get("msg_id", "")
        inbox = self._store.agent_subdir(self._session_id, self._agent_id, "inbox")
        inbox.mkdir(parents=True, exist_ok=True)

        dest = inbox / f"{msg_id}.json"
        if dest.exists():
            return  # Already exists (race with Syncthing or concurrent writer)

        msg = {
            "session_id": self._session_id,
            "from": event.get("from", ""),
            "to": self._agent_id,
            "subject": event.get("subject", ""),
            "body": event.get("body", ""),
            "kind": event.get("kind", "TASK"),
            "msg_id": msg_id,
            "created_at": event.get("created_at", ""),
        }
        tmp = inbox / f".tmp-{msg_id}.json"
        try:
            with open(tmp, "w") as f:
                f.write(json.dumps(msg, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(dest))
        except OSError:
            log.exception("SwarmReceiver: failed to write msg_id=%s to inbox", msg_id)
            tmp.unlink(missing_ok=True)

    def _is_msg_on_disk(self, msg_id: str) -> bool:
        """Check if msg_id already exists in inbox, processing, or archive."""
        for sub in ("inbox", "processing", "archive"):
            path = self._store.agent_subdir(self._session_id, self._agent_id, sub)
            if (path / f"{msg_id}.json").exists():
                return True
        return False

    def _try_ack(self, msg_id: str) -> None:
        """Best-effort ack (consumed) for a message."""
        if not msg_id:
            return
        try:
            self._kernel.ack(self._session_id, self._agent_id, msg_id, "consumed")
        except Exception:
            log.debug("SwarmReceiver: ack failed for msg_id=%s (may not be in processing)", msg_id)

    # ── Watch mode (local filesystem polling) ──────────────────────────

    def start_watch(
        self,
        mailbox_root: Path,
        poll_interval: float = DEFAULT_WATCH_POLL_INTERVAL,
    ) -> None:
        """Start watching the local inbox directory for new messages.

        Uses stat-based mtime/size polling — no inotify or watchdog
        dependency.  Works with or without Syncthing (scanning catches
        synced files).

        Parameters
        ----------
        mailbox_root : Path
            Root mailbox directory (contains ``session_id/agent_id/inbox/``).
        poll_interval : float
            Seconds between filesystem scans.
        """
        if self._started:
            raise RuntimeError("receiver already started")
        self._started = True
        self._stop_event.clear()
        self._watch_mailbox_root = mailbox_root
        self._watch_poll_interval = poll_interval

        # Build initial stat cache so we only fire for *new* files
        self._build_initial_stat_cache()

        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            daemon=True,
            name=f"receiver-watch-{self._agent_id}",
        )
        self._watch_thread.start()

    def _build_initial_stat_cache(self) -> None:
        """Snapshot current inbox files so we don't fire for pre-existing ones."""
        inbox = self._store.agent_subdir(self._session_id, self._agent_id, "inbox")
        if not inbox.exists():
            return
        for f in inbox.iterdir():
            if f.is_file() and f.suffix == ".json" and not f.name.startswith("."):
                try:
                    st = f.stat()
                    self._stat_cache[f.name] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    pass

    def _watch_loop(self) -> None:
        """Background thread: poll inbox directory for new files."""
        while not self._stop_event.is_set():
            try:
                new_msgs = self._scan_inbox()
                for msg in new_msgs:
                    self._handle_watch_message(msg)
            except Exception:
                log.exception("SwarmReceiver: watch scan error")

            # Sleep in small increments so stop() is responsive
            remaining = self._watch_poll_interval
            while remaining > 0 and not self._stop_event.is_set():
                time.sleep(min(remaining, _LOOP_TICK))
                remaining -= _LOOP_TICK

    def _scan_inbox(self) -> list[dict]:
        """Scan inbox for new or changed files. Returns list of message dicts."""
        inbox = self._store.agent_subdir(self._session_id, self._agent_id, "inbox")
        if not inbox.exists():
            return []

        new_msgs: list[dict] = []
        for f in inbox.iterdir():
            if not f.is_file() or f.suffix != ".json" or f.name.startswith("."):
                continue
            try:
                st = f.stat()
            except OSError:
                continue

            key = f.name
            cached = self._stat_cache.get(key)
            current = (st.st_mtime_ns, st.st_size)

            if cached is not None and cached == current:
                continue  # unchanged

            # New or changed file
            self._stat_cache[key] = current

            try:
                msg = json.loads(f.read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue

            msg_id = msg.get("msg_id", f.stem)

            # Dedup
            if msg_id in self._seen_msg_ids:
                continue
            self._seen_msg_ids.add(msg_id)

            new_msgs.append(msg)

        return new_msgs

    def _handle_watch_message(self, msg: dict) -> None:
        """Process a message discovered by watch: fire callbacks, ack."""
        self._fire_callbacks(msg)

        msg_id = msg.get("msg_id", "")
        self._try_ack(msg_id)

    # ── Callback dispatch ──────────────────────────────────────────────

    def _fire_callbacks(self, msg: dict) -> None:
        """Fire all registered callbacks whose filters match the message."""
        for callback, channels, kinds in self._callbacks:
            # Apply channel filter
            if channels:
                msg_channel = msg.get("channel_id", "")
                if msg_channel not in channels:
                    continue
            # Apply kind filter
            if kinds:
                if msg.get("kind", "") not in kinds:
                    continue
            try:
                callback(msg)
            except Exception:
                log.exception("SwarmReceiver: callback error")

    # ── Control ────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Signal the receiver to stop and wait for threads to finish."""
        self._stop_event.set()
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=5)
        if self._stream_thread is not None:
            self._stream_thread.join(timeout=5)
        self._started = False

    @property
    def is_running(self) -> bool:
        """True if the receiver background thread is still alive."""
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return True
        if self._stream_thread is not None and self._stream_thread.is_alive():
            return True
        return False
