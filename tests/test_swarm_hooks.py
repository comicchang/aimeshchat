"""Tests for swarm hook lifecycle (Oracle P1-5).

Covers:
- on_agent_start registers; on_agent_stop actually unregisters
- on_agent_stop without prior start → no-op (reason 'never registered')
- reset() clears registered set + kernel
- double-stop safe
- OMP runner cleanup logs warning on hook failure (no silent swallow)
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from codeagent.hooks import swarm_hooks
from codeagent.hooks.swarm_hooks import (
    _get_kernel,
    on_agent_start,
    on_agent_stop,
    reset,
)
from codeagent.swarm.model import AgentLocation


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_hooks():
    """Ensure module-level state is fresh for every test."""
    reset()
    yield
    reset()


@pytest.fixture
def kernel_with_session(tmp_path):
    """Return a kernel with a pre-created session (s1, mgr, [w1, w2])."""
    kernel, _ = _get_kernel(store_root=tmp_path)
    kernel.create_session("s1", "mgr", ["w1", "w2"])
    return kernel


# ── on_agent_start + on_agent_stop ───────────────────────────────────────


class TestRegisterUnregister:
    """on_agent_start registers; on_agent_stop unregisters."""

    def test_start_registers_and_stop_unregisters(self, tmp_path, kernel_with_session):
        kernel = kernel_with_session

        # register via hook
        result = on_agent_start(
            session_id="s1", agent_id="w1",
            host_alias="__local__", backend="omp",
            store_root=tmp_path,
        )
        assert result["agent_id"] == "w1"
        assert result["session_id"] == "s1"

        # verify routing entry exists
        loc = kernel._routing.get(("s1", "w1"))
        assert loc is not None
        assert loc.agent_id == "w1"

        # unregister via hook
        stop_result = on_agent_stop(session_id="s1", agent_id="w1",
                                    store_root=tmp_path)
        assert stop_result["unregistered"] is True

        # verify routing entry removed
        assert kernel._routing.get(("s1", "w1")) is None

        # verify _registered tracking cleared
        assert ("s1", "w1") not in swarm_hooks._registered


# ── on_agent_stop without prior start ────────────────────────────────────


class TestStopNeverRegistered:
    """on_agent_stop for an agent that was never started is a safe no-op."""

    def test_stop_without_start_returns_reason(self, tmp_path, kernel_with_session):
        # Do NOT call on_agent_start first
        result = on_agent_stop(session_id="s1", agent_id="w1",
                               store_root=tmp_path)
        assert result["unregistered"] is False
        assert result["reason"] == "never registered"
        assert result["agent_id"] == "w1"
        assert result["session_id"] == "s1"

    def test_stop_without_start_does_not_touch_routing(self, tmp_path, kernel_with_session):
        """Unregistering a never-registered agent must not raise or modify routing."""
        # Register w1 first, then try to stop w2 (never registered)
        on_agent_start(session_id="s1", agent_id="w1",
                       store_root=tmp_path)

        result = on_agent_stop(session_id="s1", agent_id="w2",
                               store_root=tmp_path)
        assert result["unregistered"] is False
        # w1 still registered
        assert ("s1", "w1") in swarm_hooks._registered


# ── reset() ──────────────────────────────────────────────────────────────


class TestReset:
    """reset() clears kernel, store, store_root, and _registered."""

    def test_reset_clears_kernel_and_registered(self, tmp_path, kernel_with_session):
        on_agent_start(session_id="s1", agent_id="w1",
                       store_root=tmp_path)
        on_agent_start(session_id="s1", agent_id="w2",
                       store_root=tmp_path)

        assert swarm_hooks._kernel is not None
        assert ("s1", "w1") in swarm_hooks._registered
        assert ("s1", "w2") in swarm_hooks._registered

        reset()

        assert swarm_hooks._kernel is None
        assert swarm_hooks._store is None
        assert swarm_hooks._store_root is None
        assert len(swarm_hooks._registered) == 0

    def test_reset_allows_fresh_start(self, tmp_path, kernel_with_session):
        """After reset, a new kernel can be created for a different store_root."""
        on_agent_start(session_id="s1", agent_id="w1",
                       store_root=tmp_path)

        old_kernel = swarm_hooks._kernel
        reset()

        # New store_root, new kernel — must not be the pre-reset object
        new_path = tmp_path / "other"
        kernel2, _ = _get_kernel(store_root=new_path)
        assert kernel2 is not old_kernel
        assert swarm_hooks._store_root == new_path


# ── double-stop safe ────────────────────────────────────────────────────


class TestDoubleStop:
    """Calling on_agent_stop twice is safe and idempotent."""

    def test_double_stop_returns_noop_second_time(self, tmp_path, kernel_with_session):
        on_agent_start(session_id="s1", agent_id="w1",
                       store_root=tmp_path)

        first = on_agent_stop(session_id="s1", agent_id="w1",
                              store_root=tmp_path)
        assert first["unregistered"] is True

        second = on_agent_stop(session_id="s1", agent_id="w1",
                               store_root=tmp_path)
        assert second["unregistered"] is False
        assert second["reason"] == "never registered"


# ── store_root change recreates kernel ──────────────────────────────────


class TestStoreRootChange:
    """Different store_root triggers kernel recreation."""

    def test_kernel_recreated_on_store_root_change(self, tmp_path, kernel_with_session):
        kernel1 = swarm_hooks._kernel

        # Same store_root → same kernel
        kernel_again, _ = _get_kernel(store_root=tmp_path)
        assert kernel_again is kernel1

        # Different store_root → new kernel
        new_path = tmp_path / "alt"
        kernel2, _ = _get_kernel(store_root=new_path)
        assert kernel2 is not kernel1
        assert swarm_hooks._store_root == new_path


# ── OMP runner cleanup logging ──────────────────────────────────────────


class TestOMPRunnerCleanupLogging:
    """OMP _cleanup logs warning on hook failure (no silent swallow)."""

    def test_cleanup_logs_warning_on_hook_failure(self, caplog):
        """When on_agent_stop raises, _cleanup must log a WARNING, not swallow."""
        import codeagent.runners.omp as omp_mod

        runner = omp_mod.OMPRunner()
        # Simulate that _parse_output set swarm attributes
        runner._swarm_session_id = "bad-session"
        runner._swarm_agent_id = "bad-agent"

        with mock.patch(
            "codeagent.runners.omp.on_agent_stop",
            side_effect=RuntimeError("kernel exploded"),
        ):
            with caplog.at_level(logging.WARNING, logger="codeagent.runners.omp"):
                runner._cleanup()

        assert any("on_agent_stop hook failed" in r.message for r in caplog.records)
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_cleanup_does_not_swallow_silently(self, caplog):
        """The old ``except Exception: pass`` around on_agent_stop must be gone."""
        import codeagent.runners.omp as omp_mod
        import inspect

        source = inspect.getsource(omp_mod.OMPRunner._cleanup)
        # hook 调用分支必须 LOG.warning，不得静默吞掉
        assert "LOG.warning" in source, "_cleanup does not log on hook failure"
        # 找 on_agent_stop 调用后的 except 块，确认不是 pass
        hook_block = source.split("on_agent_stop(")[1].split("except")[1]
        assert "pass" not in hook_block, "hook failure still swallowed silently"
