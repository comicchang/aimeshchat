"""Tests for codeagent.tcp.spool — durable WAL for TCP forwarding."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codeagent.tcp.spool import SpoolEntry, SpoolStore


# ── helpers ─────────────────────────────────────────────────────────────


def _make_entry(
    session_id: str = "s1",
    host_alias: str = "host-a",
    from_id: str = "main",
    to_id: str = "agent1",
    msg_id: str | None = None,
) -> SpoolEntry:
    """Build a minimal SpoolEntry for testing."""
    import uuid as _uuid

    if msg_id is None:
        msg_id = f"mid-{_uuid.uuid4().hex[:8]}"
    return SpoolEntry(
        uuid=_uuid.uuid4().hex,
        session_id=session_id,
        from_id=from_id,
        to_id=to_id,
        msg_id=msg_id,
        payload={"kind": "direct", "msg_id": msg_id, "body": "hello"},
        created_at=time.time(),
        host_alias=host_alias,
    )


# ── write / ack / deliver cycle ────────────────────────────────────────


class TestWriteAckCycle:
    def test_write_persists_json(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        entry = _make_entry()
        path = store.write(entry)

        assert path.exists()
        assert path.name.endswith(".json")
        data = json.loads(path.read_text())
        assert data["uuid"] == entry.uuid

    def test_ack_renames_to_acked(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        entry = _make_entry()
        store.write(entry)

        acked_path = store.ack(entry.uuid, entry.session_id, entry.host_alias)
        assert acked_path.exists()
        assert acked_path.name.endswith(".json.acked")
        # original .json should be gone
        assert not (acked_path.parent / f"{entry.uuid}.json").exists()

    def test_fail_renames_to_failed(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        entry = _make_entry()
        store.write(entry)

        failed_path = store.fail(entry.uuid, entry.session_id, entry.host_alias)
        assert failed_path.exists()
        assert failed_path.name.endswith(".json.failed")

    def test_ack_nonexistent_raises(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        with pytest.raises(FileNotFoundError):
            store.ack("no-such-uuid", "s1", "host-a")

    def test_fail_nonexistent_raises(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        with pytest.raises(FileNotFoundError):
            store.fail("no-such-uuid", "s1", "host-a")

    def test_full_deliver_cycle(self, tmp_path: Path) -> None:
        """write → pending lists it → ack removes from pending."""
        store = SpoolStore(tmp_path)
        entry = _make_entry()
        store.write(entry)

        pending = store.pending(entry.session_id, entry.host_alias)
        assert len(pending) == 1
        assert pending[0].uuid == entry.uuid
        assert pending[0].status == "pending"

        store.ack(entry.uuid, entry.session_id, entry.host_alias)
        pending = store.pending(entry.session_id, entry.host_alias)
        assert len(pending) == 0


# ── crash-recover replay ───────────────────────────────────────────────


class TestReplay:
    def test_replay_returns_pending_entries(self, tmp_path: Path) -> None:
        """Simulate a restart by creating a new SpoolStore on the same root."""
        store1 = SpoolStore(tmp_path)
        e1 = _make_entry(session_id="s1", host_alias="host-a")
        e2 = _make_entry(session_id="s1", host_alias="host-b")
        e3 = _make_entry(session_id="s2", host_alias="host-a")
        store1.write(e1)
        store1.write(e2)
        store1.write(e3)
        # ack e3 — it should NOT appear in replay
        store1.ack(e3.uuid, e3.session_id, e3.host_alias)

        # "restart"
        store2 = SpoolStore(tmp_path)
        recovered = store2.replay()

        uuids = {e.uuid for e in recovered}
        assert e1.uuid in uuids
        assert e2.uuid in uuids
        assert e3.uuid not in uuids  # was acked
        assert len(recovered) == 2

    def test_replay_empty_root(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path / "empty")
        assert store.replay() == []

    def test_replay_sorted_by_created_at(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        e1 = _make_entry()
        e1.created_at = 100.0
        e2 = _make_entry()
        e2.created_at = 50.0
        store.write(e1)
        store.write(e2)

        recovered = store.replay()
        assert recovered[0].created_at <= recovered[1].created_at


# ── expiry / cleanup ───────────────────────────────────────────────────


class TestCleanup:
    def test_cleanup_removes_old_acked(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        entry = _make_entry()
        store.write(entry)
        store.ack(entry.uuid, entry.session_id, entry.host_alias)

        # make the file appear old by touching mtime
        acked_file = (
            tmp_path / ".spool" / entry.session_id / entry.host_alias
        ) / f"{entry.uuid}.json.acked"
        old_time = time.time() - 7200
        import os

        os.utime(acked_file, (old_time, old_time))

        removed = store.cleanup(max_age_seconds=3600)
        assert removed == 1
        assert not acked_file.exists()

    def test_cleanup_keeps_fresh_acked(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        entry = _make_entry()
        store.write(entry)
        store.ack(entry.uuid, entry.session_id, entry.host_alias)

        removed = store.cleanup(max_age_seconds=3600)
        assert removed == 0

    def test_cleanup_removes_old_failed(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        entry = _make_entry()
        store.write(entry)
        store.fail(entry.uuid, entry.session_id, entry.host_alias)

        failed_file = (
            tmp_path / ".spool" / entry.session_id / entry.host_alias
        ) / f"{entry.uuid}.json.failed"
        old_time = time.time() - 7200
        import os

        os.utime(failed_file, (old_time, old_time))

        removed = store.cleanup(max_age_seconds=3600)
        assert removed == 1

    def test_cleanup_never_removes_pending(self, tmp_path: Path) -> None:
        """Pending entries must survive cleanup regardless of age."""
        store = SpoolStore(tmp_path)
        entry = _make_entry()
        store.write(entry)

        # age the pending file
        pending_file = (
            tmp_path / ".spool" / entry.session_id / entry.host_alias
        ) / f"{entry.uuid}.json"
        old_time = time.time() - 99999
        import os

        os.utime(pending_file, (old_time, old_time))

        removed = store.cleanup(max_age_seconds=1)
        assert removed == 0
        assert pending_file.exists()


# ── concurrent writes ──────────────────────────────────────────────────


class TestConcurrency:
    def test_concurrent_writes_unique_entries(self, tmp_path: Path) -> None:
        """ThreadPoolExecutor should produce distinct files with no collision."""
        store = SpoolStore(tmp_path)
        entries = [_make_entry(host_alias="shared-host") for _ in range(20)]

        def _write_one(e: SpoolEntry) -> Path:
            return store.write(e)

        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(_write_one, entries))

        # all 20 files should exist
        assert all(p.exists() for p in paths)
        # no two share a name
        assert len({p.name for p in paths}) == 20
        # all visible via pending()
        pending = store.pending("s1", "shared-host")
        assert len(pending) == 20


# ── corrupt entry handling ─────────────────────────────────────────────


class TestCorruptEntries:
    def test_corrupt_json_skipped_in_pending(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        e1 = _make_entry(host_alias="h1")
        store.write(e1)

        # inject a corrupt file
        host_dir = tmp_path / ".spool" / e1.session_id / "h1"
        (host_dir / "bad-uuid.json").write_text("NOT JSON {{{", encoding="utf-8")

        pending = store.pending(e1.session_id, "h1")
        # only the good entry returned
        assert len(pending) == 1
        assert pending[0].uuid == e1.uuid

    def test_corrupt_json_skipped_in_replay(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        e1 = _make_entry(host_alias="h1")
        store.write(e1)

        # inject corrupt file in another host dir
        corrupt_dir = tmp_path / ".spool" / "sX" / "bad-host"
        corrupt_dir.mkdir(parents=True)
        (corrupt_dir / "corrupt.json").write_text("{broken", encoding="utf-8")

        recovered = store.replay()
        assert len(recovered) == 1
        assert recovered[0].uuid == e1.uuid

    def test_tmp_file_ignored(self, tmp_path: Path) -> None:
        """Stale .tmp- files from a crash mid-write must be invisible."""
        store = SpoolStore(tmp_path)
        e1 = _make_entry(host_alias="h1")
        store.write(e1)

        host_dir = tmp_path / ".spool" / e1.session_id / "h1"
        (host_dir / ".tmp-crash.json").write_text('{"stale": true}', encoding="utf-8")

        pending = store.pending(e1.session_id, "h1")
        assert len(pending) == 1
        assert pending[0].uuid == e1.uuid


# ── host_alias isolation ───────────────────────────────────────────────


class TestHostAliasIsolation:
    def test_pending_scoped_to_host_alias(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        e_a = _make_entry(host_alias="alpha")
        e_b = _make_entry(host_alias="beta")
        store.write(e_a)
        store.write(e_b)

        alpha_pending = store.pending(e_a.session_id, "alpha")
        beta_pending = store.pending(e_b.session_id, "beta")

        assert len(alpha_pending) == 1
        assert alpha_pending[0].host_alias == "alpha"
        assert len(beta_pending) == 1
        assert beta_pending[0].host_alias == "beta"

    def test_ack_only_affects_correct_host(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        e_a = _make_entry(host_alias="alpha")
        e_b = _make_entry(host_alias="beta")
        store.write(e_a)
        store.write(e_b)

        store.ack(e_a.uuid, e_a.session_id, "alpha")

        assert store.pending(e_a.session_id, "alpha") == []
        assert len(store.pending(e_b.session_id, "beta")) == 1

    def test_replay_collects_all_hosts(self, tmp_path: Path) -> None:
        store = SpoolStore(tmp_path)
        e_a = _make_entry(host_alias="alpha")
        e_b = _make_entry(host_alias="beta")
        e_c = _make_entry(session_id="s2", host_alias="alpha")
        store.write(e_a)
        store.write(e_b)
        store.write(e_c)

        recovered = store.replay()
        assert len(recovered) == 3


# ── SpoolEntry serialisation ───────────────────────────────────────────


class TestSpoolEntry:
    def test_roundtrip_json(self) -> None:
        entry = _make_entry()
        rt = SpoolEntry.from_json(entry.to_json())
        assert rt.uuid == entry.uuid
        assert rt.payload == entry.payload
        assert rt.status == entry.status

    def test_from_dict_ignores_extra_keys(self) -> None:
        d = _make_entry().to_dict()
        d["unknown_field"] = 42
        entry = SpoolEntry.from_dict(d)
        assert entry.uuid == d["uuid"]
