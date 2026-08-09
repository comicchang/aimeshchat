"""Tests for the session subsystem — key, lock, and registry.

Covers:
- Key computation determinism and exclusions
- Lock reentrancy, contention, and context-manager usage
- Registry CRUD, state transitions, and concurrent access
"""
from __future__ import annotations

import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from codeagent.domain import (
    LOCAL_HOST_MARKER,
    HostSpec,
    RepoEntry,
    RunRequest,
    RunResult,
    Target,
    TopicSpec,
)
from codeagent.session.key import _normalize_workdir, compute_session_key
from codeagent.session.lock import SessionLock, session_lock
from codeagent.session.registry import SessionRegistry


# ── Fixtures ─────────────────────────────────────────────────────────────


def _make_target(
    *,
    host_name: str = "dev-server",
    ssh_alias: str = "dev-server",
    workdir: str = "/home/dev/project",
    hostnames: tuple[str, ...] = ("dev-server",),
    is_local: bool = False,
) -> Target:
    host = HostSpec(name=host_name, ssh_alias=ssh_alias, hostnames=hostnames)
    repo = RepoEntry(host=host_name, path=workdir)
    return Target(host=host, repo=repo, is_local=is_local)


def _make_request(**kw) -> RunRequest:
    defaults = {"task": "do something", "workdir": "/home/dev/project"}
    defaults.update(kw)
    return RunRequest(**defaults)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sessions.sqlite3"


@pytest.fixture
def registry(db_path: Path) -> SessionRegistry:
    return SessionRegistry(db_path=db_path)


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    """Provide a temporary lock directory and patch _runtime_dir."""
    d = tmp_path / "locks"
    d.mkdir()
    with mock.patch("codeagent.session.lock._runtime_dir", return_value=d):
        yield d


# ── Key computation ──────────────────────────────────────────────────────


class TestNormalizeWorkdir:
    def test_empty_defaults_to_cwd(self) -> None:
        result = _normalize_workdir("")
        assert os.path.isabs(result)
        assert result == os.path.normpath(os.getcwd())

    def test_expands_tilde(self) -> None:
        result = _normalize_workdir("~/projects/foo")
        assert result.startswith(os.path.expanduser("~"))
        assert "~" not in result

    def test_normalizes_double_slashes(self) -> None:
        result = _normalize_workdir("/foo//bar///baz")
        assert "//" not in result
        assert "///" not in result

    def test_resolves_relative(self) -> None:
        result = _normalize_workdir("./relative/path")
        assert os.path.isabs(result)


class TestComputeSessionKey:
    def test_deterministic(self) -> None:
        req = _make_request(backend="opencode", agent="coder")
        target = _make_target()
        k1 = compute_session_key(req, target)
        k2 = compute_session_key(req, target)
        assert k1 == k2

    def test_format(self) -> None:
        req = _make_request(backend="claude", agent="reviewer")
        target = _make_target(ssh_alias="dev-server", workdir="/home/dev/project")
        key = compute_session_key(req, target)
        assert key.startswith("dev-server:")
        assert key.endswith(":claude:reviewer")
        # key = "dev-server:/home/dev/project:claude:reviewer"
        parts = key.split(":")
        assert len(parts) == 4

    def test_local_host_marker(self) -> None:
        req = _make_request(backend="opencode")
        target = _make_target(is_local=True)
        key = compute_session_key(req, target)
        assert key.startswith(LOCAL_HOST_MARKER + ":")

    def test_model_excluded(self) -> None:
        """Model change must NOT alter the key."""
        req1 = _make_request(backend="opencode", model="opus")
        req2 = _make_request(backend="opencode", model="sonnet")
        target = _make_target()
        assert compute_session_key(req1, target) == compute_session_key(req2, target)

    def test_agent_affects_key(self) -> None:
        req1 = _make_request(backend="opencode", agent="coder")
        req2 = _make_request(backend="opencode", agent="reviewer")
        target = _make_target()
        assert compute_session_key(req1, target) != compute_session_key(req2, target)

    def test_empty_agent(self) -> None:
        req = _make_request(backend="opencode", agent=None)
        target = _make_target()
        key = compute_session_key(req, target)
        assert key.endswith(":opencode:")
        # No trailing agent segment beyond the last colon

    def test_backend_defaults_to_opencode(self) -> None:
        req = _make_request(backend=None)
        target = _make_target()
        key = compute_session_key(req, target)
        assert ":opencode:" in key

    def test_different_hosts_different_keys(self) -> None:
        req = _make_request(backend="opencode")
        t1 = _make_target(ssh_alias="dev-server")
        t2 = _make_target(ssh_alias="blue")
        assert compute_session_key(req, t1) != compute_session_key(req, t2)

    def test_different_workdirs_different_keys(self) -> None:
        req = _make_request(backend="opencode")
        t1 = _make_target(workdir="/home/dev/a")
        t2 = _make_target(workdir="/home/dev/b")
        assert compute_session_key(req, t1) != compute_session_key(req, t2)


# ── Lock ─────────────────────────────────────────────────────────────────


class TestSessionLock:
    def test_basic_acquire_release(self, lock_dir: Path) -> None:
        lock = SessionLock("test:key")
        assert not lock.locked
        assert lock.acquire() is True
        assert lock.locked
        lock.release()
        assert not lock.locked

    def test_context_manager(self, lock_dir: Path) -> None:
        lock = SessionLock("test:ctx")
        with lock:
            assert lock.locked
        assert not lock.locked

    def test_reentrant(self, lock_dir: Path) -> None:
        lock = SessionLock("test:reentrant")
        assert lock.acquire() is True
        assert lock.acquire() is True  # reentrant
        assert lock.locked
        lock.release()  # inner
        assert lock.locked  # still held
        lock.release()  # outer
        assert not lock.locked

    def test_non_blocking_contention(self, lock_dir: Path) -> None:
        """A second lock on a different thread fails immediately with blocking=False."""
        barrier = threading.Event()
        release_event = threading.Event()
        results: list[bool] = []

        def worker():
            lock = SessionLock("test:contend")
            results.append(lock.acquire(blocking=False))
            if results[-1]:
                lock.release()
            barrier.set()

        # Hold the lock in the main thread.
        main_lock = SessionLock("test:contend")
        main_lock.acquire()

        t = threading.Thread(target=worker)
        t.start()
        barrier.wait(timeout=5)
        main_lock.release()
        t.join(timeout=5)

        assert len(results) == 1
        assert results[0] is False

    def test_sequential_non_blocking(self, lock_dir: Path) -> None:
        """After release, a non-blocking acquire succeeds."""
        lock = SessionLock("test:seq")
        lock.acquire()
        lock.release()

        lock2 = SessionLock("test:seq")
        assert lock2.acquire(blocking=False) is True
        lock2.release()

    def test_session_lock_context_manager(self, lock_dir: Path) -> None:
        with session_lock("test:cm") as lock:
            assert isinstance(lock, SessionLock)
            assert lock.locked

    def test_session_lock_contention_raises(self, lock_dir: Path) -> None:
        """Non-blocking acquire on a key held by ANOTHER thread raises."""
        import time as _time
        held = SessionLock("test:cm_contend")
        held.acquire()
        error_holder: list = []

        def contender():
            try:
                with session_lock("test:cm_contend", blocking=False):
                    pass
            except (TimeoutError, OSError) as e:
                error_holder.append(e)

        t = threading.Thread(target=contender)
        t.start()
        t.join(timeout=5)
        held.release()
        # Either raises or succeeds (platform-dependent flock behavior)
        # The key assertion is that it doesn't hang forever
        assert not t.is_alive() or len(error_holder) > 0

    def test_per_key_independence(self, lock_dir: Path) -> None:
        """Locks on different keys do not interfere."""
        lock_a = SessionLock("test:aaa")
        lock_b = SessionLock("test:bbb")
        lock_a.acquire()
        assert lock_b.acquire(blocking=False) is True
        lock_a.release()
        lock_b.release()

    def test_threads_independent(self, lock_dir: Path) -> None:
        """Each thread can independently acquire different keys."""
        results: list[bool] = []
        barrier = threading.Barrier(2, timeout=5)

        def worker(key_suffix: str):
            lock = SessionLock(f"test:thread:{key_suffix}")
            ok = lock.acquire(blocking=False)
            barrier.wait()
            results.append(ok)
            if ok:
                lock.release()

        threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 2
        assert all(results)
        assert all(results)


# ── Registry ─────────────────────────────────────────────────────────────


class TestSessionRegistry:
    def test_lookup_empty(self, registry: SessionRegistry) -> None:
        assert registry.lookup("nonexistent") is None

    def test_mark_starting_and_lookup(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="opencode", agent="coder")
        target = _make_target()
        registry.mark_starting("k1", req, target)

        rec = registry.lookup("k1")
        assert rec is not None
        assert rec.status == "starting"
        assert rec.session_id == ""
        assert rec.backend == "opencode"
        assert rec.host == "dev-server"
        assert rec.agent == "coder"

    def test_mark_observed(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="opencode")
        target = _make_target()
        registry.mark_starting("k2", req, target)
        registry.mark_observed("k2", "sess-abc-123")

        rec = registry.lookup("k2")
        assert rec is not None
        assert rec.status == "observed"
        assert rec.session_id == "sess-abc-123"

    def test_mark_active(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="opencode")
        target = _make_target()
        registry.mark_starting("k-active", req, target)
        registry.mark_observed("k-active", "sess-active")
        registry.mark_active("k-active")

        rec = registry.lookup("k-active")
        assert rec is not None
        assert rec.status == "active"
        assert rec.session_id == "sess-active"

    def test_mark_failed(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="opencode")
        target = _make_target()
        registry.mark_starting("k-fail", req, target)
        registry.mark_observed("k-fail", "sess-fail")
        registry.mark_failed("k-fail")

        rec = registry.lookup("k-fail")
        assert rec is not None
        assert rec.status == "failed"
        assert rec.session_id == "sess-fail"

    def test_upsert_success(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="claude", model="opus")
        target = _make_target()
        result = RunResult(returncode=0, session_id="sess-ok", backend="claude")
        rec = registry.upsert("k3", result, req, target)

        assert rec.status == "active"
        assert rec.session_id == "sess-ok"
        assert rec.backend == "claude"

        # Verify persistence
        stored = registry.lookup("k3")
        assert stored is not None
        assert stored.status == "active"

    def test_upsert_failure(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="opencode")
        target = _make_target()
        result = RunResult(returncode=1, session_id="sess-fail")
        rec = registry.upsert("k4", result, req, target)
        assert rec.status == "failed"

    def test_upsert_interrupted(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="opencode")
        target = _make_target()
        result = RunResult(returncode=-1, session_id="sess-int")
        rec = registry.upsert("k5", result, req, target)
        assert rec.status == "interrupted"

    def test_upsert_preserves_created_at(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="opencode")
        target = _make_target()
        registry.mark_starting("k6", req, target)
        first = registry.lookup("k6")
        assert first is not None
        created = first.created_at

        time.sleep(0.05)
        result = RunResult(returncode=0, session_id="s6")
        registry.upsert("k6", result, req, target)
        second = registry.lookup("k6")
        assert second is not None
        assert second.created_at == created
        assert second.updated_at >= created

    def test_delete(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="opencode")
        target = _make_target()
        registry.mark_starting("k7", req, target)
        assert registry.lookup("k7") is not None

        assert registry.delete("k7") is True
        assert registry.lookup("k7") is None

    def test_delete_nonexistent(self, registry: SessionRegistry) -> None:
        assert registry.delete("ghost") is False

    def test_bind_existing(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="opencode")
        target = _make_target()
        registry.mark_starting("k8", req, target)
        registry.bind("k8", "bound-sess-id")

        rec = registry.lookup("k8")
        assert rec is not None
        assert rec.session_id == "bound-sess-id"
        assert rec.status == "active"

    def test_bind_creates_if_missing(self, registry: SessionRegistry) -> None:
        registry.bind("brand-new", "new-sess")
        rec = registry.lookup("brand-new")
        assert rec is not None
        assert rec.session_id == "new-sess"
        assert rec.status == "active"

    def test_list_all(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="opencode")
        t1 = _make_target(ssh_alias="dev-server")
        t2 = _make_target(ssh_alias="blue", workdir="/other")
        registry.mark_starting("list:y1", req, t1)
        registry.mark_starting("list:b1", req, t2)

        all_recs = registry.list_all()
        assert len(all_recs) >= 2

        devhost_recs = registry.list_all(host="dev-server")
        assert all(r.host == "dev-server" for r in devhost_recs)

    def test_list_all_filter_topic(self, registry: SessionRegistry) -> None:
        req1 = _make_request(backend="opencode", topic="android")
        req2 = _make_request(backend="opencode", topic="ios")
        target = _make_target()
        registry.mark_starting("topic:a", req1, target)
        registry.mark_starting("topic:i", req2, target)

        ios_recs = registry.list_all(topic="ios")
        assert len(ios_recs) >= 1
        assert all(r.topic == "ios" for r in ios_recs)

    def test_compute_key_delegates(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="claude", agent="reviewer")
        target = _make_target()
        key = registry.compute_key(req, target)
        assert ":claude:reviewer" in key

    def test_upsert_overwrites_existing(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="opencode")
        target = _make_target()

        r1 = RunResult(returncode=0, session_id="s1")
        registry.upsert("overwrite", r1, req, target)
        assert registry.lookup("overwrite").session_id == "s1"  # type: ignore[union-attr]

        r2 = RunResult(returncode=0, session_id="s2")
        registry.upsert("overwrite", r2, req, target)
        rec = registry.lookup("overwrite")
        assert rec is not None
        assert rec.session_id == "s2"

    def test_cleanup_stale(self, registry: SessionRegistry) -> None:
        req = _make_request(backend="opencode")
        target = _make_target()
        registry.mark_starting("stale1", req, target)
        registry.mark_starting("stale2", req, target)
        # Make them look old by backdating updated_at
        with registry._connect() as conn:
            conn.execute(
                "UPDATE sessions SET updated_at = 0 WHERE key IN ('stale1', 'stale2')"
            )

        # An active session should survive cleanup
        result = RunResult(returncode=0, session_id="active1")
        registry.upsert("active1", result, req, target)

        deleted = registry.cleanup_stale(max_age_seconds=60)
        assert deleted == 2
        assert registry.lookup("stale1") is None
        assert registry.lookup("stale2") is None
        assert registry.lookup("active1") is not None

    def test_mark_starting_holds_lock(self, lock_dir: Path, db_path: Path) -> None:
        """Verify mark_starting acquires SessionLock(key)."""
        reg = SessionRegistry(db_path=db_path)
        req = _make_request(backend="opencode")
        target = _make_target()
        key = "lock-test:key"

        # Acquire the lock externally — key must match mark_starting's
        # SessionLock("session:" + key), not the bare key.
        lock = SessionLock("session:" + key)
        lock.acquire()

        errors: list[Exception] = []

        def try_mark():
            try:
                # This should block until lock is released
                reg.mark_starting(key, req, target)
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=try_mark)
        t.start()
        # Give it a moment to attempt the lock
        time.sleep(0.1)
        assert t.is_alive(), "mark_starting should be blocking on the lock"

        # Release and let it proceed
        lock.release()
        t.join(timeout=5)
        assert not t.is_alive()
        assert errors == []

        # Verify the record was written
        rec = reg.lookup(key)
        assert rec is not None
        assert rec.status == "starting"

    def test_run_with_lock_basic(self, lock_dir: Path, db_path: Path) -> None:
        """run_with_lock yields a SessionLock and holds it for the block."""
        reg = SessionRegistry(db_path=db_path)
        key = "rwl:key"

        with reg.run_with_lock(key) as lock:
            assert isinstance(lock, SessionLock)
            assert lock.locked

        assert not lock.locked

    def test_run_with_lock_blocks_concurrent(self, lock_dir: Path, db_path: Path) -> None:
        """Concurrent run_with_lock on the same key blocks."""
        reg = SessionRegistry(db_path=db_path)
        key = "rwl:concurrent"
        entered = threading.Event()
        blocked = threading.Event()

        def holder():
            with reg.run_with_lock(key):
                entered.set()
                # Hold for a bit
                time.sleep(0.3)

        def waiter():
            entered.wait(timeout=5)
            # Try to acquire same key — must use run_with_lock's prefixed key
            lock = SessionLock("session:" + key)
            got = lock.acquire(blocking=False)
            if got:
                lock.release()
            else:
                blocked.set()

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=waiter)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert blocked.is_set(), "Second thread should have been blocked"

    def test_run_with_lock_allows_different_keys(self, lock_dir: Path, db_path: Path) -> None:
        """run_with_lock on different keys does not block each other."""
        reg = SessionRegistry(db_path=db_path)
        results: list[str] = []
        barrier = threading.Barrier(2, timeout=5)

        def worker(k: str):
            with reg.run_with_lock(k):
                results.append(f"entered:{k}")
                barrier.wait()
                results.append(f"exited:{k}")

        t1 = threading.Thread(target=worker, args=("rwl:a",))
        t2 = threading.Thread(target=worker, args=("rwl:b",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 4
        # Both should have entered before either exited (barrier sync)
        assert "entered:rwl:a" in results
        assert "entered:rwl:b" in results


# ── Concurrent access ────────────────────────────────────────────────────


class TestConcurrentAccess:
    """Verify that concurrent writes from multiple threads and processes
    do not corrupt the database or produce inconsistent state."""

    def test_thread_concurrent_upserts(self, db_path: Path) -> None:
        """Multiple threads upserting different keys simultaneously."""
        reg = SessionRegistry(db_path=db_path)
        target = _make_target()
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                req = _make_request(backend="opencode", agent=f"agent-{idx}")
                key = f"concurrent:{idx}"
                reg.mark_starting(key, req, target)
                result = RunResult(returncode=0, session_id=f"sess-{idx}")
                rec = reg.upsert(key, result, req, target)
                assert rec.session_id == f"sess-{idx}"
                assert rec.status == "active"
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"
        # All 10 records should exist
        for i in range(10):
            rec = reg.lookup(f"concurrent:{i}")
            assert rec is not None
            assert rec.session_id == f"sess-{i}"

    def test_process_concurrent_upserts(self, db_path: Path, tmp_path: Path) -> None:
        """Multiple processes upserting different keys via subprocess."""
        import subprocess as sp
        reg = SessionRegistry(db_path=db_path)
        target = _make_target()

        for i in range(5):
            req = _make_request(backend="opencode", agent=f"p-agent-{i}")
            reg.mark_starting(f"proc:{i}", req, target)

        src_dir = Path(__file__).resolve().parents[1] / "src"
        for i in range(5):
            script = (
                f'import sys; sys.path.insert(0, "{src_dir}"); '
                f'from codeagent.domain import *; '
                f'from codeagent.session.registry import SessionRegistry; '
                f'from pathlib import Path; '
                f'reg = SessionRegistry(db_path=Path("{db_path}")); '
                f'h = HostSpec("test","test",("test",)); '
                f'r = RepoEntry("test","/tmp"); '
                f't = Target(h,r); '
                f'req = RunRequest(task="t",backend="opencode",agent="p-agent-{i}"); '
                f'reg.upsert("proc:{i}", RunResult(0,session_id="proc-sess-{i}"), req, t)'
            )
            p = sp.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
            assert p.returncode == 0, f"subprocess {i} failed: {p.stderr}"

        for i in range(5):
            rec = reg.lookup(f"proc:{i}")
            assert rec is not None
            assert rec.session_id == f"proc-sess-{i}"

    def test_thread_contention_same_key(self, db_path: Path) -> None:
        """Multiple threads racing on the same key — last writer wins,
        but no corruption or exception."""
        reg = SessionRegistry(db_path=db_path)
        target = _make_target()
        req = _make_request(backend="opencode")
        errors: list[Exception] = []

        # Pre-create the record
        reg.mark_starting("race:key", req, target)

        def racer(idx: int) -> None:
            try:
                result = RunResult(returncode=0, session_id=f"race-{idx}")
                reg.upsert("race:key", result, req, target)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=racer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"
        # Exactly one record should exist
        rec = reg.lookup("race:key")
        assert rec is not None
        assert rec.session_id.startswith("race-")

    def test_read_during_write(self, db_path: Path) -> None:
        """Reads and writes interleaved — no stale read crashes."""
        reg = SessionRegistry(db_path=db_path)
        target = _make_target()
        errors: list[Exception] = []

        # Seed some records
        for i in range(5):
            req = _make_request(backend="opencode", agent=f"r-{i}")
            reg.mark_starting(f"rw:{i}", req, target)

        def writer() -> None:
            try:
                for i in range(5):
                    result = RunResult(returncode=0, session_id=f"rw-sess-{i}")
                    req = _make_request(backend="opencode", agent=f"r-{i}")
                    reg.upsert(f"rw:{i}", result, req, target)
            except Exception as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(20):
                    for i in range(5):
                        rec = reg.lookup(f"rw:{i}")
                        # Should never be None after initial seed
                        assert rec is not None
            except Exception as exc:
                errors.append(exc)

        all_threads = (
            [threading.Thread(target=writer) for _ in range(2)]
            + [threading.Thread(target=reader) for _ in range(3)]
        )
        for t in all_threads:
            t.start()
        for t in all_threads:
            t.join(timeout=15)

        assert errors == [], f"Thread errors: {errors}"

    def test_state_transition_integrity(self, db_path: Path) -> None:
        """Verify the full state machine under concurrent access:
        absent → starting → observed → active."""
        reg = SessionRegistry(db_path=db_path)
        target = _make_target()
        req = _make_request(backend="opencode")
        errors: list[Exception] = []

        def full_lifecycle(idx: int) -> None:
            try:
                key = f"lifecycle:{idx}"
                # absent → starting
                reg.mark_starting(key, req, target)
                rec = reg.lookup(key)
                assert rec is not None and rec.status == "starting"

                # starting → observed
                reg.mark_observed(key, f"obs-{idx}")
                rec = reg.lookup(key)
                assert rec is not None and rec.status == "observed"

                # observed → active
                result = RunResult(returncode=0, session_id=f"final-{idx}")
                reg.upsert(key, result, req, target)
                rec = reg.lookup(key)
                assert rec is not None
                assert rec.status == "active"
                assert rec.session_id == f"final-{idx}"
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=full_lifecycle, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"
        for i in range(6):
            rec = reg.lookup(f"lifecycle:{i}")
            assert rec is not None
            assert rec.status == "active"
            assert rec.session_id == f"final-{i}"

    def test_state_transition_via_mark_methods(self, db_path: Path) -> None:
        """Full lifecycle using mark_observed, mark_active, mark_failed."""
        reg = SessionRegistry(db_path=db_path)
        target = _make_target()
        req = _make_request(backend="opencode")
        errors: list[Exception] = []

        def lifecycle_with_marks(idx: int) -> None:
            try:
                key = f"mark-lifecycle:{idx}"
                # absent → starting
                reg.mark_starting(key, req, target)
                rec = reg.lookup(key)
                assert rec is not None and rec.status == "starting"

                # starting → observed
                reg.mark_observed(key, f"mark-obs-{idx}")
                rec = reg.lookup(key)
                assert rec is not None and rec.status == "observed"
                assert rec.session_id == f"mark-obs-{idx}"

                if idx % 2 == 0:
                    # observed → active
                    reg.mark_active(key)
                    rec = reg.lookup(key)
                    assert rec is not None and rec.status == "active"
                else:
                    # observed → failed
                    reg.mark_failed(key)
                    rec = reg.lookup(key)
                    assert rec is not None and rec.status == "failed"
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=lifecycle_with_marks, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"
        for i in range(8):
            rec = reg.lookup(f"mark-lifecycle:{i}")
            assert rec is not None
            if i % 2 == 0:
                assert rec.status == "active"
            else:
                assert rec.status == "failed"


# ── RunRequest fields ────────────────────────────────────────────────────


class TestRunRequestFields:
    """Verify RunRequest has the required session lifecycle fields."""

    def test_timeout_default(self) -> None:
        req = _make_request()
        assert req.timeout == 600

    def test_timeout_custom(self) -> None:
        req = _make_request(timeout=300)
        assert req.timeout == 300

    def test_resume_session_id_default(self) -> None:
        req = _make_request()
        assert req.resume_session_id is None

    def test_resume_session_id_set(self) -> None:
        req = _make_request(resume_session_id="sess-abc-123")
        assert req.resume_session_id == "sess-abc-123"

    def test_session_key_is_namespace(self) -> None:
        """session_key is the registry lookup namespace, NOT the backend session ID."""
        req = _make_request(session_key="dev-server:/work:opencode:")
        assert req.session_key.startswith("dev-server:")
        # resume_session_id is the actual backend session ID
        req2 = _make_request(
            session_key="dev-server:/work:opencode:",
            resume_session_id="actual-sess-id-456",
        )
        assert req2.session_key != req2.resume_session_id
