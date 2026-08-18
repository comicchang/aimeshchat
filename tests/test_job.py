"""Tests for codeagent.job — background job execution with file-based status.

Covers the full public API (get_manager, JobInfo, JobManager) plus the
private helpers (_run_job, _read_info, _iso_now, _atomic_write) with real
filesystem access via tmp_path — file operations are NOT mocked, matching
the tests/test_cli.py style (pytest + unittest.mock only where a branch
is otherwise unreachable from the filesystem).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import pytest

import codeagent.job as job_mod
from codeagent.domain import RunResult
from codeagent.job import JobInfo, JobManager, _atomic_write, _iso_now, get_manager

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
# Larger than any possible pid on macOS/Linux → os.kill(pid, 0) always ESRCH.
_DEAD_PID = 2**31 - 1


def _make_manager(tmp_path: Path) -> JobManager:
    """Build an isolated JobManager rooted under tmp_path."""
    return JobManager(jobs_root=tmp_path / "jobs")


# ── get_manager ─────────────────────────────────────────────────────────


class TestGetManager:
    """Process-wide singleton."""

    def test_singleton_returns_same_instance(self, monkeypatch, tmp_path):
        monkeypatch.setattr(job_mod, "state_dir", lambda: tmp_path)
        monkeypatch.setattr(job_mod, "_manager", None)
        m1 = get_manager()
        m2 = get_manager()
        assert m1 is m2
        assert isinstance(m1, JobManager)
        assert m1._root == tmp_path / "jobs"
        assert m1._root.is_dir()

    def test_new_instance_after_reset(self, monkeypatch, tmp_path):
        monkeypatch.setattr(job_mod, "state_dir", lambda: tmp_path)
        monkeypatch.setattr(job_mod, "_manager", None)
        m1 = get_manager()
        monkeypatch.setattr(job_mod, "_manager", None)
        m2 = get_manager()
        assert m2 is not m1


# ── JobInfo ─────────────────────────────────────────────────────────────


class TestJobInfo:
    """Public view of a background job."""

    def test_to_dict_all_fields(self):
        info = JobInfo(
            job_id="abc123",
            status="done",
            task="task",
            host="host",
            workdir="/w",
            created_at="2026-01-01T00:00:00Z",
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
            returncode=0,
            stdout="out",
            stderr="err",
            session_id="sess",
            backend="local",
            pid=42,
        )
        assert info.to_dict() == {
            "job_id": "abc123",
            "status": "done",
            "task": "task",
            "host": "host",
            "workdir": "/w",
            "created_at": "2026-01-01T00:00:00Z",
            "started_at": "2026-01-01T00:00:01Z",
            "finished_at": "2026-01-01T00:00:02Z",
            "returncode": 0,
            "stdout": "out",
            "stderr": "err",
            "session_id": "sess",
            "backend": "local",
            "pid": 42,
        }

    def test_to_dict_defaults_keep_none(self):
        d = JobInfo(job_id="j", status="pending").to_dict()
        assert set(d) == {
            "job_id", "status", "task", "host", "workdir", "created_at",
            "started_at", "finished_at", "returncode", "stdout", "stderr",
            "session_id", "backend", "pid",
        }
        assert d["returncode"] is None
        assert d["session_id"] is None
        assert d["pid"] is None


# ── JobManager init ─────────────────────────────────────────────────────


class TestJobManagerInit:
    """Constructor behavior."""

    def test_init_creates_root(self, tmp_path):
        root = tmp_path / "jobs"
        mgr = JobManager(jobs_root=root)
        assert root.is_dir()
        assert mgr._root == root
        assert mgr._pool is threading.Thread
        assert mgr._active == {}

    def test_init_default_root_uses_state_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(job_mod, "state_dir", lambda: tmp_path)
        mgr = JobManager()
        assert mgr._root == tmp_path / "jobs"
        assert mgr._root.is_dir()

    def test_init_existing_root_reused(self, tmp_path):
        root = tmp_path / "jobs"
        root.mkdir(parents=True)
        sentinel = root / "keep.txt"
        sentinel.write_text("x")
        JobManager(jobs_root=root)
        assert sentinel.exists()


# ── create_placeholder ──────────────────────────────────────────────────


class TestCreatePlaceholder:
    """Job dir + meta.json + status=pending."""

    def test_creates_meta_and_status(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder(task="deploy", host="h1", workdir="/srv")
        assert re.fullmatch(r"[0-9a-f]{12}", job_id)
        d = mgr._root / job_id
        assert d.is_dir()
        meta = json.loads((d / "meta.json").read_text())
        assert meta["job_id"] == job_id
        assert meta["task"] == "deploy"
        assert meta["host"] == "h1"
        assert meta["workdir"] == "/srv"
        assert _ISO_RE.fullmatch(meta["created_at"])
        assert (d / "status").read_text() == "pending"

    def test_task_truncated_to_500(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder(task="x" * 600)
        meta = json.loads((mgr._root / job_id / "meta.json").read_text())
        assert len(meta["task"]) == 500

    def test_defaults_empty(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder()
        meta = json.loads((mgr._root / job_id / "meta.json").read_text())
        assert meta["task"] == ""
        assert meta["host"] == ""
        assert meta["workdir"] == ""
        assert (mgr._root / job_id / "status").read_text() == "pending"


# ── mark_running ────────────────────────────────────────────────────────


class TestMarkRunning:
    """Status → running, started_at + pid files."""

    def test_marks_running_with_pid(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder()
        mgr.mark_running(job_id, pid=4321)
        d = mgr._root / job_id
        assert (d / "status").read_text() == "running"
        assert _ISO_RE.fullmatch((d / "started_at").read_text().strip())
        assert (d / "pid").read_text() == "4321"

    def test_no_pid_file_when_zero(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder()
        mgr.mark_running(job_id)
        d = mgr._root / job_id
        assert (d / "status").read_text() == "running"
        assert (d / "started_at").exists()
        assert not (d / "pid").exists()

    def test_missing_job_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(FileNotFoundError):
            mgr.mark_running("nope")


# ── mark_done ───────────────────────────────────────────────────────────


class TestMarkDone:
    """result.json, status, finished_at, stderr tail."""

    def test_done_success(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder()
        result = RunResult(
            returncode=0, stdout="hello", stderr="warn",
            session_id="s-1", backend="local", host="h", workdir="/w",
        )
        mgr.mark_done(job_id, result)
        d = mgr._root / job_id
        assert (d / "status").read_text() == "done"
        assert _ISO_RE.fullmatch((d / "finished_at").read_text().strip())
        rd = json.loads((d / "result.json").read_text())
        assert rd == {
            "returncode": 0, "stdout": "hello", "stderr": "warn",
            "session_id": "s-1", "backend": "local", "host": "h", "workdir": "/w",
        }
        assert (d / "stderr.txt").read_text() == "warn"

    def test_failed_when_nonzero(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder()
        mgr.mark_done(job_id, RunResult(returncode=2, stdout="", stderr="boom"))
        assert (mgr._root / job_id / "status").read_text() == "failed"

    def test_no_stderr_file_when_empty(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder()
        mgr.mark_done(job_id, RunResult(returncode=0, stdout="ok"))
        d = mgr._root / job_id
        assert not (d / "stderr.txt").exists()

    def test_stderr_tail_truncated_to_max(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder()
        big = "x" * 20000
        mgr.mark_done(job_id, RunResult(returncode=0, stderr=big))
        tail = (mgr._root / job_id / "stderr.txt").read_bytes()
        assert len(tail) == job_mod._MAX_STDERR_TAIL
        assert tail == big.encode()[-job_mod._MAX_STDERR_TAIL:]

    def test_missing_job_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(FileNotFoundError):
            mgr.mark_done("nope", RunResult(returncode=0))


# ── status ──────────────────────────────────────────────────────────────


class TestStatus:
    """status() reads JobInfo from disk."""

    def test_returns_job_info(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder(task="t", host="h")
        mgr.mark_running(job_id, pid=os.getpid())
        info = mgr.status(job_id)
        assert isinstance(info, JobInfo)
        assert info.job_id == job_id
        assert info.status == "running"
        assert info.task == "t"
        assert info.host == "h"
        assert info.pid == os.getpid()

    def test_missing_job_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(FileNotFoundError):
            mgr.status("missing")


# ── list_jobs ───────────────────────────────────────────────────────────


class TestListJobs:
    """Recent jobs, newest first."""

    def test_empty_root(self, tmp_path):
        mgr = JobManager(jobs_root=tmp_path / "jobs")
        assert mgr.list_jobs() == []

    def test_root_deleted_returns_empty(self, tmp_path):
        mgr = JobManager(jobs_root=tmp_path / "jobs")
        shutil.rmtree(mgr._root)
        assert mgr.list_jobs() == []

    def test_newest_first_and_limit(self, tmp_path):
        mgr = _make_manager(tmp_path)
        older = mgr.create_placeholder(task="older")
        newer = mgr.create_placeholder(task="newer")
        # Distinct explicit mtimes make ordering deterministic.
        os.utime(mgr._root / older, (1_000_000, 1_000_000))
        os.utime(mgr._root / newer, (2_000_000, 2_000_000))
        assert [i.job_id for i in mgr.list_jobs()] == [newer, older]
        assert [i.job_id for i in mgr.list_jobs(limit=1)] == [newer]

    def test_ignores_dirs_without_status(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder(task="real")
        (mgr._root / "stray").mkdir()
        infos = mgr.list_jobs()
        assert [i.job_id for i in infos] == [job_id]

    def test_skips_stat_errors(self, monkeypatch, tmp_path):
        """A job dir whose mtime stat() fails is skipped, not fatal."""
        mgr = _make_manager(tmp_path)
        target = mgr._root / mgr.create_placeholder(task="x")
        orig_stat = Path.stat
        call_count = {"n": 0}

        def _stat(self, *args, **kwargs):
            # Let is_dir()/exists() (internal stat) work normally;
            # only the explicit d.stat() for mtime should fail.
            if self == target:
                call_count["n"] += 1
                if call_count["n"] > 1:
                    raise OSError("boom")
            return orig_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _stat)
        assert mgr.list_jobs() == []


# ── wait ────────────────────────────────────────────────────────────────


class TestWait:
    """Blocking wait — thread join for thread jobs, poll for child jobs."""

    def test_thread_job_wait_done(self, tmp_path):
        mgr = _make_manager(tmp_path)

        def fn(*, greeting="hi"):
            time.sleep(0.05)
            return RunResult(returncode=0, stdout=greeting, backend="local")

        job_id = mgr.submit(fn, task="t", greeting="hello")
        info = mgr.wait(job_id, timeout=10)
        assert info.status == "done"
        assert info.returncode == 0
        assert info.stdout == "hello"
        assert info.backend == "local"

    def test_thread_job_wait_join_timeout(self, tmp_path):
        mgr = _make_manager(tmp_path)

        def slow():
            time.sleep(0.4)
            return RunResult(returncode=0)

        job_id = mgr.submit(slow)
        info = mgr.wait(job_id, timeout=0.05)
        assert info.status == "running"
        done = mgr.wait(job_id, timeout=5)
        assert done.status == "done"

    def test_subprocess_polling(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder(task="child")
        d = mgr._root / job_id

        def finish():
            time.sleep(0.1)
            (d / "result.json").write_text(json.dumps({
                "returncode": 0, "stdout": "child-out", "stderr": "",
                "session_id": None, "backend": "local", "host": "", "workdir": "",
            }))
            (d / "status").write_text("done")
            (d / "finished_at").write_text("2026-01-01T00:00:00Z")

        t = threading.Thread(target=finish)
        t.start()
        try:
            info = mgr.wait(job_id, timeout=5)
            assert info.status == "done"
            assert info.returncode == 0
            assert info.stdout == "child-out"
        finally:
            t.join(timeout=5)

    def test_subprocess_poll_timeout_returns_current(self, tmp_path):
        mgr = _make_manager(tmp_path)
        job_id = mgr.create_placeholder()
        info = mgr.wait(job_id, timeout=0.1)
        assert info.status == "pending"

    def test_wait_missing_job_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(FileNotFoundError):
            mgr.wait("nosuch", timeout=5)


# ── submit ──────────────────────────────────────────────────────────────


class TestSubmit:
    """Background thread submission."""

    def test_submit_returns_id_and_runs(self, tmp_path):
        mgr = _make_manager(tmp_path)

        def fn(*, msg):
            return RunResult(returncode=0, stdout=msg)

        job_id = mgr.submit(fn, task="sub", host="h", workdir="w", msg="hello")
        assert re.fullmatch(r"[0-9a-f]{12}", job_id)
        d = mgr._root / job_id
        meta = json.loads((d / "meta.json").read_text())
        assert meta["task"] == "sub"
        assert meta["host"] == "h"
        assert meta["workdir"] == "w"
        info = mgr.wait(job_id, timeout=10)
        assert info.status == "done"
        assert info.stdout == "hello"
        assert json.loads((d / "result.json").read_text())["stdout"] == "hello"

    def test_submit_registers_active_thread(self, tmp_path):
        mgr = _make_manager(tmp_path)

        def slow():
            time.sleep(0.2)
            return RunResult(returncode=0)

        job_id = mgr.submit(slow)
        t = mgr._active.get(job_id)
        assert t is not None
        assert t.name == f"job-{job_id}"
        assert t.daemon
        mgr.wait(job_id, timeout=10)
        assert job_id not in mgr._active

    def test_submit_exception_marks_failed(self, tmp_path):
        mgr = _make_manager(tmp_path)

        def boom():
            raise RuntimeError("kaboom")

        job_id = mgr.submit(boom)
        info = mgr.wait(job_id, timeout=10)
        assert info.status == "failed"
        assert info.returncode == 1
        assert "job exception: kaboom" in info.stderr


# ── _run_job (thread target) ────────────────────────────────────────────


class TestRunJob:
    """Direct thread-target execution + persistence."""

    def test_success(self, tmp_path):
        mgr = _make_manager(tmp_path)
        d = tmp_path / "j1"
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"job_id": "j1"}))
        (d / "status").write_text("pending")
        mgr._active["j1"] = threading.Thread()
        result = RunResult(
            returncode=0, stdout="out", stderr="err", session_id="sid",
            backend="b", host="h", workdir="w",
        )
        mgr._run_job("j1", d, lambda **kw: result)
        assert (d / "status").read_text() == "done"
        assert _ISO_RE.fullmatch((d / "started_at").read_text().strip())
        assert _ISO_RE.fullmatch((d / "finished_at").read_text().strip())
        assert (d / "stderr.txt").read_text() == "err"
        rd = json.loads((d / "result.json").read_text())
        assert rd["returncode"] == 0
        assert rd["stdout"] == "out"
        assert rd["session_id"] == "sid"
        assert "j1" not in mgr._active

    def test_exception(self, tmp_path):
        mgr = _make_manager(tmp_path)
        d = tmp_path / "j2"
        d.mkdir()
        (d / "meta.json").write_text("{}")
        (d / "status").write_text("pending")

        def boom():
            raise ValueError("nope")

        mgr._run_job("j2", d, boom)
        assert (d / "status").read_text() == "failed"
        info = mgr._read_info("j2", d)
        assert info.returncode == 1
        assert "job exception: nope" in info.stderr
        assert (d / "stderr.txt").read_text() == info.stderr


# ── _read_info (disk reconstruction) ────────────────────────────────────


class TestReadInfo:
    """Full reconstruction, missing files, corrupt files, stale pid."""

    def test_full_reconstruction(self, tmp_path):
        mgr = _make_manager(tmp_path)
        d = tmp_path / "job1"
        d.mkdir()
        (d / "status").write_text("done\n")
        (d / "meta.json").write_text(json.dumps({
            "job_id": "job1", "task": "t", "host": "h", "workdir": "w",
            "created_at": "2026-01-01T00:00:00Z",
        }))
        (d / "started_at").write_text("2026-01-01T00:00:01Z\n")
        (d / "finished_at").write_text("2026-01-01T00:00:02Z")
        (d / "pid").write_text("999999")
        (d / "result.json").write_text(json.dumps({
            "returncode": 3, "stdout": "out", "stderr": "err",
            "session_id": "s", "backend": "local", "host": "h", "workdir": "w",
        }))
        info = mgr._read_info("job1", d)
        assert info.status == "done"  # stripped of newline
        assert info.task == "t" and info.host == "h" and info.workdir == "w"
        assert info.created_at == "2026-01-01T00:00:00Z"
        assert info.started_at == "2026-01-01T00:00:01Z"
        assert info.finished_at == "2026-01-01T00:00:02Z"
        assert info.returncode == 3
        assert info.stdout == "out" and info.stderr == "err"
        assert info.session_id == "s" and info.backend == "local"
        # pid file is ignored for terminal statuses
        assert info.pid is None

    def test_missing_files_defaults(self, tmp_path):
        mgr = _make_manager(tmp_path)
        d = tmp_path / "empty"
        d.mkdir()
        info = mgr._read_info("empty", d)
        assert info.status == "unknown"
        assert info.task == "" and info.host == "" and info.workdir == ""
        assert info.created_at == ""
        assert info.started_at == "" and info.finished_at == ""
        assert info.returncode is None
        assert info.stdout == "" and info.stderr == ""
        assert info.session_id is None and info.backend == ""
        assert info.pid is None

    def test_status_file_as_directory(self, tmp_path):
        """Unreadable status → status stays 'unknown'."""
        mgr = _make_manager(tmp_path)
        d = tmp_path / "weird"
        d.mkdir()
        (d / "status").mkdir()  # read_text raises IsADirectoryError (OSError)
        info = mgr._read_info("weird", d)
        assert info.status == "unknown"

    def test_corrupt_meta_and_result(self, tmp_path):
        mgr = _make_manager(tmp_path)
        d = tmp_path / "corrupt"
        d.mkdir()
        (d / "status").write_text("done")
        (d / "meta.json").write_text("{not json")
        (d / "started_at").write_text("s")
        (d / "finished_at").write_text("f")
        (d / "result.json").write_text("!!!")
        info = mgr._read_info("corrupt", d)
        assert info.status == "done"
        assert info.task == "" and info.created_at == ""
        assert info.started_at == "s" and info.finished_at == "f"
        assert info.returncode is None
        assert info.stdout == "" and info.stderr == ""
        assert info.session_id is None and info.backend == ""

    def test_started_at_as_directory(self, tmp_path):
        """Unreadable started_at → left at default, not fatal."""
        mgr = _make_manager(tmp_path)
        d = tmp_path / "d1"
        d.mkdir()
        (d / "status").write_text("running")
        (d / "started_at").mkdir()
        info = mgr._read_info("d1", d)
        assert info.status == "running"
        assert info.started_at == ""

    def test_pid_alive(self, tmp_path):
        mgr = _make_manager(tmp_path)
        d = tmp_path / "alive"
        d.mkdir()
        (d / "status").write_text("running")
        (d / "pid").write_text(str(os.getpid()))
        info = mgr._read_info("alive", d)
        assert info.pid == os.getpid()
        assert info.status == "running"

    def test_pid_dead_marks_stale(self, tmp_path):
        mgr = _make_manager(tmp_path)
        d = tmp_path / "stale1"
        d.mkdir()
        (d / "status").write_text("running")
        (d / "pid").write_text(str(_DEAD_PID))
        info = mgr._read_info("stale1", d)
        assert info.status == "stale"
        assert info.pid is None

    def test_pid_invalid_marks_stale(self, tmp_path):
        mgr = _make_manager(tmp_path)
        d = tmp_path / "stale2"
        d.mkdir()
        (d / "status").write_text("running")
        (d / "pid").write_text("abc")
        info = mgr._read_info("stale2", d)
        assert info.status == "stale"
        assert info.pid is None

    def test_pid_dead_pending_stays_pending(self, tmp_path):
        """Stale is only derived from 'running', not 'pending'."""
        mgr = _make_manager(tmp_path)
        d = tmp_path / "pend"
        d.mkdir()
        (d / "status").write_text("pending")
        (d / "pid").write_text(str(_DEAD_PID))
        info = mgr._read_info("pend", d)
        assert info.status == "pending"
        assert info.pid is None


# ── utilities ───────────────────────────────────────────────────────────


class TestUtils:
    """_iso_now and _atomic_write."""

    def test_iso_now_format(self):
        assert _ISO_RE.fullmatch(_iso_now())

    def test_atomic_write_creates_file(self, tmp_path):
        p = tmp_path / "meta.json"
        _atomic_write(p, "hello")
        assert p.read_text() == "hello"
        assert not p.with_suffix(".tmp").exists()

    def test_atomic_write_overwrites(self, tmp_path):
        p = tmp_path / "meta.json"
        _atomic_write(p, "one")
        _atomic_write(p, "two")
        assert p.read_text() == "two"

    def test_atomic_write_write_then_rename(self, tmp_path, monkeypatch):
        p = tmp_path / "meta.json"
        real_replace = os.replace
        seen = []

        def fake_replace(src, dst):
            # tmp file must be fully written before rename happens.
            assert Path(src).read_text() == "payload"
            seen.append((Path(src), Path(dst)))
            real_replace(src, dst)

        monkeypatch.setattr(job_mod.os, "replace", fake_replace)
        _atomic_write(p, "payload")
        assert seen == [(p.with_suffix(".tmp"), p)]
        assert p.read_text() == "payload"

    def test_atomic_write_missing_parent_propagates(self, tmp_path):
        """No non-atomic fallback: a failed write must propagate."""
        p = tmp_path / "no" / "such" / "file.txt"
        with pytest.raises(FileNotFoundError):
            _atomic_write(p, "x")
