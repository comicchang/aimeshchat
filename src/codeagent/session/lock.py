"""Per-key file locking via flock.

Lock file path: $XDG_RUNTIME_DIR/aimeshchat/locks/<key-hash>.lock
Fallback: ~/.local/runtime/aimeshchat/locks/<key-hash>.lock

The lock covers the entire agent turn — acquired before starting a run,
released after the runner exits.  DB transactions are short; the lock is
held for the full duration of the subprocess.

Usage::

    with SessionLock("my:key"):
        # … critical section …
        pass

The lock is reentrant per-process: acquiring the same key twice in the
same process is a no-op (the flock fd is reused).
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


def _runtime_dir() -> Path:
    """Resolve the runtime directory for lock files.

    Prefers $XDG_RUNTIME_DIR, then falls back to ~/.local/runtime.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "aimeshchat" / "locks"
    return Path.home() / ".local" / "runtime" / "aimeshchat" / "locks"


def _lock_path(key: str) -> Path:
    """Hash the key to a filesystem-safe filename."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return _runtime_dir() / f"{digest}.lock"


class _LockState:
    """Per-thread, per-key lock state."""
    __slots__ = ("fd", "depth")

    def __init__(self) -> None:
        self.fd: int = -1
        self.depth: int = 0


class SessionLock:
    """Per-key advisory file lock backed by flock(2).

    Thread-safe: each thread tracks its own acquisition state.
    Reentrant within the same thread (nested ``acquire()`` calls increment
    a counter; only the outermost ``release()`` actually unlocks).

    Not fork-safe: the fd tracking is per-process; forked children must
    re-acquire.
    """

    _local = threading.local()

    def __init__(self, key: str) -> None:
        self._key = key
        self._path = _lock_path(key)

    # -- Context manager -------------------------------------------------------

    def __enter__(self) -> SessionLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    # -- Public API ------------------------------------------------------------

    def acquire(self, *, blocking: bool = True) -> bool:
        """Acquire the lock.

        Parameters
        ----------
        blocking:
            If True (default), block until the lock is available.
            If False, return immediately — True if acquired, False if
            already held by another process.

        Returns
        -------
        True if the lock was acquired, False only when ``blocking=False``
        and the lock is contended.
        """
        state = self._get_state()

        # Reentrant: already held by this thread.
        if state.depth > 0:
            state.depth += 1
            return True

        # Ensure the lock directory exists.
        self._path.parent.mkdir(parents=True, exist_ok=True)

        fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            op = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, op)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise

        state.fd = fd
        state.depth = 1
        return True

    def release(self) -> None:
        """Release the lock.  No-op if not held by this thread."""
        state = self._get_state()
        if state.depth == 0:
            return

        state.depth -= 1
        if state.depth == 0:
            fd = state.fd
            state.fd = -1
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @property
    def locked(self) -> bool:
        """True if this thread currently holds the lock."""
        return self._get_state().depth > 0

    # -- Internals -------------------------------------------------------------

    def _get_state(self) -> _LockState:
        """Per-thread lock state for this key."""
        states = getattr(self._local, "states", None)
        if states is None:
            states = {}
            self._local.states = states
        state = states.get(self._key)
        if state is None:
            state = _LockState()
            states[self._key] = state
        return state


@contextmanager
def session_lock(key: str, *, blocking: bool = True) -> Generator[SessionLock, None, None]:
    """Convenience context manager that yields a SessionLock.

    Raises ``TimeoutError`` when ``blocking=False`` and the lock is contended.
    """
    lock = SessionLock(key)
    acquired = lock.acquire(blocking=blocking)
    if not acquired:
        raise TimeoutError(f"session lock contended: {key}")
    try:
        yield lock
    finally:
        lock.release()
