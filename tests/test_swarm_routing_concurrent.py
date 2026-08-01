"""P0-4 pins: subprocess-based concurrency tests for SwarmKernel routing.

These tests verify that fcntl file locks properly serialize cross-process
writes to swarm-meta.json. The existing thread-based test in test_swarm_kernel.py
does NOT test this because threads share the same file descriptor table,
so fcntl locks don't serialize them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from codeagent.mailbox.store import MailboxStore
from codeagent.swarm.kernel import SwarmKernel
from codeagent.swarm.model import AgentLocation


# ── Helper scripts for subprocess execution ────────────────────────────

_REGISTER_SCRIPT = textwrap.dedent("""\
    import sys
    from pathlib import Path
    from codeagent.mailbox.store import MailboxStore
    from codeagent.swarm.kernel import SwarmKernel
    from codeagent.swarm.model import AgentLocation
    
    session_dir = Path(sys.argv[1])
    agent_id = sys.argv[2]
    host_alias = sys.argv[3]
    
    store = MailboxStore(root=session_dir)
    k = SwarmKernel(store=store)
    k.register(AgentLocation(agent_id=agent_id, host_alias=host_alias, backend="cli"), "s1")
    print("ok")
""")

_CREATE_CHANNEL_SCRIPT = textwrap.dedent("""\
    import sys
    from pathlib import Path
    from codeagent.mailbox.store import MailboxStore
    from codeagent.swarm.kernel import SwarmKernel
    
    session_dir = Path(sys.argv[1])
    channel_id = sys.argv[2]
    members = sys.argv[3].split(",")
    
    store = MailboxStore(root=session_dir)
    k = SwarmKernel(store=store)
    k.create_channel("s1", channel_id, members)
    print("ok")
""")

_UNREGISTER_SCRIPT = textwrap.dedent("""\
    import sys
    from pathlib import Path
    from codeagent.mailbox.store import MailboxStore
    from codeagent.swarm.kernel import SwarmKernel
    
    session_dir = Path(sys.argv[1])
    agent_id = sys.argv[2]
    
    store = MailboxStore(root=session_dir)
    k = SwarmKernel(store=store)
    k.unregister("s1", agent_id)
    print("ok")
""")


def _subprocess_env() -> dict[str, str]:
    """Build environment with PYTHONPATH pointing to the project src."""
    env = os.environ.copy()
    src_dir = str(Path.cwd() / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_dir + (os.pathsep + existing if existing else "")
    return env


# ── Tests ──────────────────────────────────────────────────────────────


class TestSubprocessRoutingConcurrency:
    """P0-4: verify fcntl locks serialize cross-process routing writes."""

    def test_two_processes_register_preserve_both(self, tmp_path: Path):
        """Two subprocesses register different agents → both survive in routing.

        Oracle critique: threads sharing one kernel can't test fcntl locks.
        Real processes are required for cross-process lock serialization.
        """
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()

        # Create session in test process (persists session.json + swarm-meta.json)
        store = MailboxStore(root=session_dir)
        k = SwarmKernel(store=store)
        k.create_session("s1", "mgr", ["w1", "w2"])

        # Spawn two subprocesses concurrently
        env = _subprocess_env()
        p1 = subprocess.Popen(
            [sys.executable, "-c", _REGISTER_SCRIPT,
             str(session_dir), "w1", "host-a"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
        p2 = subprocess.Popen(
            [sys.executable, "-c", _REGISTER_SCRIPT,
             str(session_dir), "w2", "host-b"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )

        # Wait for both
        r1 = p1.wait(timeout=30)
        r2 = p2.wait(timeout=30)

        assert r1 == 0, f"Process 1 (w1) failed:\n{p1.stderr.read()}"
        assert r2 == 0, f"Process 2 (w2) failed:\n{p2.stderr.read()}"

        # Verify both registrations survived in swarm-meta.json
        meta_path = session_dir / "s1" / "swarm-meta.json"
        assert meta_path.exists(), "swarm-meta.json missing"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        routing = meta.get("routing", {})

        assert "w1" in routing, "w1 registration lost (TOCTOU regression)"
        assert "w2" in routing, "w2 registration lost (TOCTOU regression)"
        assert routing["w1"]["host_alias"] == "host-a"
        assert routing["w2"]["host_alias"] == "host-b"

        # Verify a fresh kernel loads both from disk
        k2 = SwarmKernel(store=MailboxStore(root=session_dir))
        loc1 = k2.get_location("s1", "w1")
        loc2 = k2.get_location("s1", "w2")
        assert loc1 is not None, "w1 not loaded by fresh kernel"
        assert loc2 is not None, "w2 not loaded by fresh kernel"
        assert loc1.host_alias == "host-a"
        assert loc2.host_alias == "host-b"

    def test_register_and_create_channel_preserve_both(self, tmp_path: Path):
        """Process A registers w1, process B creates channel → both survive.

        Cross-operation concurrency: register and create_channel write to
        the same swarm-meta.json but different keys.
        """
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()

        store = MailboxStore(root=session_dir)
        k = SwarmKernel(store=store)
        k.create_session("s1", "mgr", ["w1", "w2"])

        env = _subprocess_env()

        # Process A: register w1
        p1 = subprocess.Popen(
            [sys.executable, "-c", _REGISTER_SCRIPT,
             str(session_dir), "w1", "host-a"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )

        # Process B: create channel ch-1
        p2 = subprocess.Popen(
            [sys.executable, "-c", _CREATE_CHANNEL_SCRIPT,
             str(session_dir), "ch-1", "w1,w2"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )

        r1 = p1.wait(timeout=30)
        r2 = p2.wait(timeout=30)

        assert r1 == 0, f"Register (w1) failed:\n{p1.stderr.read()}"
        assert r2 == 0, f"Create channel (ch-1) failed:\n{p2.stderr.read()}"

        # Verify both routing and channel survived
        meta_path = session_dir / "s1" / "swarm-meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

        routing = meta.get("routing", {})
        channels = meta.get("channels", {})

        assert "w1" in routing, "w1 registration lost"
        assert "ch-1" in channels, "channel ch-1 lost"
        assert routing["w1"]["host_alias"] == "host-a"
        assert channels["ch-1"]["members"] == ["w1", "w2"]

        # Verify a fresh kernel loads both
        k2 = SwarmKernel(store=MailboxStore(root=session_dir))
        assert k2.get_location("s1", "w1") is not None
        # Channels are restored via _load_persisted_sessions
        assert "ch-1" in k2._channels.get("s1", {})

    def test_unregister_removes_only_own(self, tmp_path: Path):
        """Unregister w1 → w2 remains; no data loss from concurrent writes."""
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()

        store = MailboxStore(root=session_dir)
        k = SwarmKernel(store=store)
        k.create_session("s1", "mgr", ["w1", "w2"])
        k.register(AgentLocation("w1", "host-a", "cli"), "s1")
        k.register(AgentLocation("w2", "host-b", "cli"), "s1")

        # Verify baseline
        meta_path = session_dir / "s1" / "swarm-meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        routing = meta.get("routing", {})
        assert "w1" in routing
        assert "w2" in routing

        # Spawn subprocess to unregister w1
        env = _subprocess_env()
        r = subprocess.run(
            [sys.executable, "-c", _UNREGISTER_SCRIPT,
             str(session_dir), "w1"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert r.returncode == 0, f"Unregister failed:\n{r.stderr}"

        # Verify w1 removed, w2 preserved
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        routing = meta.get("routing", {})
        assert "w1" not in routing, "w1 should have been unregistered"
        assert "w2" in routing, "w2 registration lost (data loss)"
        assert routing["w2"]["host_alias"] == "host-b"

        # Verify a fresh kernel also sees the correct state
        k2 = SwarmKernel(store=MailboxStore(root=session_dir))
        assert k2.get_location("s1", "w1") is None, "w1 should be gone"
        loc2 = k2.get_location("s1", "w2")
        assert loc2 is not None, "w2 lost after fresh load"
        assert loc2.host_alias == "host-b"
