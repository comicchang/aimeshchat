"""Background job execution — non-blocking run with file-based status.

Each job lives under ``$XDG_STATE_HOME/aimeshchat/jobs/<job_id>/``:
  - ``meta.json``   — immutable metadata (created_at, task, host, …)
  - ``status``      — one-line status: pending | running | done | failed
  - ``result.json`` — written on completion (same schema as RunResult)
  - ``stderr.txt``  — stderr tail (last 8 KiB, truncated) for quick inspection

The job directory is the lock: meta.json is written first (atomically via
rename), status transitions are single-write, and result.json is the final
artifact.  No external daemon or database required.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from codeagent.domain import RunResult
from codeagent.util.paths import state_dir

log = logging.getLogger(__name__)

# Max stderr tail kept in status dir (bytes).
_MAX_STDERR_TAIL = 8192

# Singleton — one manager per process.
_manager: Optional["JobManager"] = None
_manager_lock = threading.Lock()


def get_manager() -> "JobManager":
    """Return the process-wide ``JobManager`` singleton."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = JobManager()
    return _manager


@dataclass
class JobInfo:
    """Public view of a background job."""
    job_id: str
    status: str  # pending | running | done | failed | stale
    task: str = ""
    host: str = ""
    workdir: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    session_id: str | None = None
    backend: str = ""
    pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobManager:
    """Manages background job lifecycle in ``$XDG_STATE_HOME/aimeshchat/jobs/``."""

    def __init__(self, *, jobs_root: Path | None = None) -> None:
        self._root = jobs_root or (state_dir() / "jobs")
        self._root.mkdir(parents=True, exist_ok=True)
        self._pool = threading.Thread  # lightweight — one thread per job
        self._active: dict[str, threading.Thread] = {}

    # ── public API ─────────────────────────────────────────────────────

    def create_placeholder(
        self,
        *,
        task: str = "",
        host: str = "",
        workdir: str = "",
    ) -> str:
        """Create a job directory and return ``job_id`` (status=pending).

        Used by detached-subprocess mode where the child process writes
        its own result.
        """
        job_id = uuid4().hex[:12]
        job_dir = self._root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        now = _iso_now()
        meta = {
            "job_id": job_id,
            "task": task[:500],
            "host": host,
            "workdir": workdir,
            "created_at": now,
        }
        _atomic_write(job_dir / "meta.json", json.dumps(meta, indent=2))
        _atomic_write(job_dir / "status", "pending")
        return job_id

    def mark_running(self, job_id: str, *, pid: int = 0) -> None:
        """Mark *job_id* as running and optionally record the child PID."""
        job_dir = self._root / job_id
        if not job_dir.exists():
            raise FileNotFoundError(f"job {job_id} not found")
        _atomic_write(job_dir / "status", "running")
        _atomic_write(job_dir / "started_at", _iso_now())
        if pid:
            _atomic_write(job_dir / "pid", str(pid))

    def submit(
        self,
        execute_fn: Callable[..., RunResult],
        *,
        task: str = "",
        host: str = "",
        workdir: str = "",
        **execute_kwargs: Any,
    ) -> str:
        """Submit *execute_fn* as a background job.  Returns ``job_id``.

        *execute_fn* must accept no positional args — pass extra context
        through ``**execute_kwargs`` (they are forwarded as keyword args).
        It must return a ``RunResult``.
        """
        job_id = uuid4().hex[:12]
        job_dir = self._root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        # Write immutable metadata first.
        now = _iso_now()
        meta = {
            "job_id": job_id,
            "task": task[:500],
            "host": host,
            "workdir": workdir,
            "created_at": now,
        }
        _atomic_write(job_dir / "meta.json", json.dumps(meta, indent=2))
        _atomic_write(job_dir / "status", "pending")

        # Spawn thread.
        t = threading.Thread(
            target=self._run_job,
            args=(job_id, job_dir, execute_fn),
            kwargs=execute_kwargs,
            name=f"job-{job_id}",
            daemon=True,
        )
        self._active[job_id] = t
        t.start()
        return job_id

    def status(self, job_id: str) -> JobInfo:
        """Return current status of *job_id* (reads from disk)."""
        job_dir = self._root / job_id
        if not job_dir.exists():
            raise FileNotFoundError(f"job {job_id} not found")
        return self._read_info(job_id, job_dir)

    def list_jobs(self, *, limit: int = 20) -> list[JobInfo]:
        """List recent jobs, newest first."""
        if not self._root.exists():
            return []
        entries: list[tuple[float, str]] = []
        for d in self._root.iterdir():
            if d.is_dir() and (d / "status").exists():
                try:
                    entries.append((d.stat().st_mtime, d.name))
                except OSError:
                    continue
        entries.sort(reverse=True)
        return [self._read_info(name, self._root / name) for _, name in entries[:limit]]

    def mark_done(self, job_id: str, result: RunResult) -> None:
        """Write a final result for *job_id* (called from the child process)."""
        job_dir = self._root / job_id
        if not job_dir.exists():
            raise FileNotFoundError(f"job {job_id} not found")
        result_dict = {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "session_id": result.session_id,
            "backend": result.backend,
            "host": result.host,
            "workdir": result.workdir,
        }
        _atomic_write(job_dir / "result.json", json.dumps(result_dict, indent=2))
        if result.stderr:
            tail = result.stderr.encode("utf-8", errors="replace")[-_MAX_STDERR_TAIL:]
            (job_dir / "stderr.txt").write_bytes(tail)
        final_status = "done" if result.returncode == 0 else "failed"
        _atomic_write(job_dir / "status", final_status)
        _atomic_write(job_dir / "finished_at", _iso_now())

    def wait(self, job_id: str, *, timeout: float = 0) -> JobInfo:
        """Block until *job_id* finishes.  ``timeout=0`` = forever.

        For thread-based jobs, joins the thread.  For subprocess-based
        jobs, polls the status file (the child writes ``done``/``failed``
        when finished).
        """
        t = self._active.get(job_id)
        if t is not None:
            t.join(timeout=timeout or None)
            return self.status(job_id)

        # Subprocess-based: poll status file.
        import time as _time
        deadline = _time.monotonic() + (timeout or 3600 * 24)  # 24h default
        while _time.monotonic() < deadline:
            info = self.status(job_id)
            if info.status in ("done", "failed"):
                return info
            _time.sleep(0.5)
        return self.status(job_id)

    # ── internals ──────────────────────────────────────────────────────

    def _run_job(
        self,
        job_id: str,
        job_dir: Path,
        execute_fn: Callable[..., RunResult],
        **kwargs: Any,
    ) -> None:
        """Thread target — executes the function and persists the result."""
        _atomic_write(job_dir / "status", "running")
        _atomic_write(job_dir / "started_at", _iso_now())

        try:
            result: RunResult = execute_fn(**kwargs)
        except Exception as exc:
            log.exception("job %s failed", job_id)
            result = RunResult(returncode=1, stderr=f"job exception: {exc}")

        # Persist result.
        result_dict = {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "session_id": result.session_id,
            "backend": result.backend,
            "host": result.host,
            "workdir": result.workdir,
        }
        _atomic_write(job_dir / "result.json", json.dumps(result_dict, indent=2))

        # Truncated stderr tail for quick `cat` inspection.
        if result.stderr:
            tail = result.stderr.encode("utf-8", errors="replace")[-_MAX_STDERR_TAIL:]
            (job_dir / "stderr.txt").write_bytes(tail)

        final_status = "done" if result.returncode == 0 else "failed"
        _atomic_write(job_dir / "status", final_status)
        _atomic_write(job_dir / "finished_at", _iso_now())

        # Clean up thread ref.
        self._active.pop(job_id, None)

    def _read_info(self, job_id: str, job_dir: Path) -> JobInfo:
        """Reconstruct ``JobInfo`` from disk files."""
        info = JobInfo(job_id=job_id, status="unknown")
        try:
            info.status = (job_dir / "status").read_text().strip()
        except OSError:
            pass

        meta_path = job_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                info.task = meta.get("task", "")
                info.host = meta.get("host", "")
                info.workdir = meta.get("workdir", "")
                info.created_at = meta.get("created_at", "")
            except (json.JSONDecodeError, OSError):
                pass

        for name, attr in [("started_at", "started_at"), ("finished_at", "finished_at")]:
            p = job_dir / name
            if p.exists():
                try:
                    setattr(info, attr, p.read_text().strip())
                except OSError:
                    pass

        # Read PID (for subprocess-based jobs).
        pid_path = job_dir / "pid"
        if pid_path.exists() and info.status not in ("done", "failed"):
            try:
                pid = int(pid_path.read_text().strip())
                os.kill(pid, 0)  # raises if not alive
                info.pid = pid
            except (ValueError, OSError, ProcessLookupError):
                # Process is gone but status not yet updated — mark stale.
                if info.status == "running":
                    info.status = "stale"

        result_path = job_dir / "result.json"
        if result_path.exists():
            try:
                r = json.loads(result_path.read_text())
                info.returncode = r.get("returncode")
                info.stdout = r.get("stdout", "")
                info.stderr = r.get("stderr", "")
                info.session_id = r.get("session_id")
                info.backend = r.get("backend", "")
            except (json.JSONDecodeError, OSError):
                pass

        return info


# ── utilities ───────────────────────────────────────────────────────────


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (write-then-rename)."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        # Best-effort fallback.
        try:
            path.write_text(content, encoding="utf-8")
        except OSError:
            log.warning("failed to write %s", path)
