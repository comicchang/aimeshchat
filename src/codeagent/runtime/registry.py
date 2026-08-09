"""RuntimeRegistry — OMP → OpenCode → generic adapter selection.

Selection rules (per plan):
  - explicit runtime name unavailable → UNSUPPORTED_RUNTIME
  - implicit selection prefers OMP → OpenCode → generic, but only picks
    adapters that satisfy the required capabilities; missing capabilities
    are NEVER faked.
  - required capabilities not satisfiable → UNSUPPORTED_CAPABILITY
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from codeagent.runtime.base import (
    CAP_STREAM_EVENTS,
    RUNTIME_GENERIC,
    RUNTIME_OMP,
    RUNTIME_OPENCODE,
    RuntimeAdapter,
    RuntimeHandle,
    RuntimeErrorCode,
    UNSUPPORTED_CAPABILITY,
    UNSUPPORTED_RUNTIME,
)

log = logging.getLogger(__name__)

# Implicit preference order (highest first).
_IMPLICIT_ORDER = (RUNTIME_OMP, RUNTIME_OPENCODE, RUNTIME_GENERIC)

# Required capabilities per runtime class (fixed, verified by contract).
_RUNTIME_REQUIRED_CAPS: dict[str, frozenset[str]] = {
    RUNTIME_OMP: frozenset(),        # full-capability when interactive
    RUNTIME_OPENCODE: frozenset({CAP_STREAM_EVENTS}),
    RUNTIME_GENERIC: frozenset({CAP_STREAM_EVENTS}),
}


class RuntimeRegistry:
    """Registry of runtime adapters with capability-based selection."""

    def __init__(self) -> None:
        self._adapters: dict[str, RuntimeAdapter] = {}
        self._handles: dict[str, RuntimeHandle] = {}
        self._build_defaults()

    def _build_defaults(self) -> None:
        """Register the built-in adapters (lazy imports avoid hard deps)."""
        try:
            from codeagent.runtime.omp import OMPRuntimeAdapter

            self._adapters[RUNTIME_OMP] = OMPRuntimeAdapter()
        except Exception as exc:  # noqa: BLE001 — adapter availability is optional
            log.debug("OMP adapter unavailable: %s", exc)
        try:
            from codeagent.runtime.opencode import OpenCodeRuntimeAdapter

            self._adapters[RUNTIME_OPENCODE] = OpenCodeRuntimeAdapter()
        except Exception as exc:
            log.debug("OpenCode adapter unavailable: %s", exc)
        try:
            from codeagent.runtime.generic import GenericRuntimeAdapter

            self._adapters[RUNTIME_GENERIC] = GenericRuntimeAdapter()
        except Exception as exc:
            log.debug("Generic adapter unavailable: %s", exc)

    # ── public API ─────────────────────────────────────────────────────

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def get(
        self,
        name: Optional[str] = None,
        required_capabilities: frozenset[str] | set[str] | None = None,
    ) -> RuntimeAdapter:
        """Select an adapter by explicit name or implicit preference.

        Raises RuntimeErrorCode(UNSUPPORTED_RUNTIME) when no adapter is
        available; RuntimeErrorCode(UNSUPPORTED_CAPABILITY) when the
        selected adapter cannot satisfy required capabilities.
        """
        required = frozenset(required_capabilities or set())

        candidates: list[str]
        if name:
            if name not in self._adapters:
                raise RuntimeErrorCode(
                    UNSUPPORTED_RUNTIME,
                    f"runtime {name!r} unavailable; registered: {self.names()}",
                )
            candidates = [name]
        else:
            candidates = [n for n in _IMPLICIT_ORDER if n in self._adapters]

        for cand in candidates:
            adapter = self._adapters[cand]
            caps = self._adapter_capabilities(adapter)
            if required <= caps:
                return adapter
        if required:
            raise RuntimeErrorCode(
                UNSUPPORTED_CAPABILITY,
                f"no runtime satisfies required capabilities {sorted(required)}; "
                f"available: {{n: sorted(self._adapter_capabilities(self._adapters[n])) for n in self._adapters}}",
            )
        raise RuntimeErrorCode(UNSUPPORTED_RUNTIME, "no runtime adapter registered")

    def spawn(
        self,
        name: Optional[str],
        request: dict,
        required_capabilities: frozenset[str] | set[str] | None = None,
    ) -> RuntimeHandle:
        adapter = self.get(name, required_capabilities)
        handle = adapter.spawn(request)
        self._handles[handle.runtime_id] = handle
        return handle

    def probe(self, runtime_id: str) -> dict:
        handle = self._require_handle(runtime_id)
        adapter = self._adapters.get(handle.runtime)
        if adapter is None:
            raise RuntimeErrorCode(UNSUPPORTED_RUNTIME, f"no adapter for {handle.runtime!r}")
        return adapter.probe(handle)

    def stop(self, runtime_id: str, reason: str) -> None:
        handle = self._require_handle(runtime_id)
        adapter = self._adapters.get(handle.runtime)
        if adapter is not None:
            adapter.stop(handle, reason)
        self._handles.pop(runtime_id, None)

    def send(self, runtime_id: str, message: dict) -> dict:
        handle = self._require_handle(runtime_id)
        adapter = self._adapters.get(handle.runtime)
        if adapter is None:
            raise RuntimeErrorCode(UNSUPPORTED_RUNTIME, f"no adapter for {handle.runtime!r}")
        return adapter.send(handle, message)

    # ── internals ──────────────────────────────────────────────────────

    @staticmethod
    def _adapter_capabilities(adapter: RuntimeAdapter) -> frozenset[str]:
        """Adapters may expose a class-level ``capabilities`` set."""
        caps = getattr(adapter, "capabilities", None)
        if isinstance(caps, (frozenset, set, list, tuple)):
            return frozenset(caps)
        # Static default per runtime class.
        return _RUNTIME_REQUIRED_CAPS.get(getattr(adapter, "name", ""), frozenset())

    def _require_handle(self, runtime_id: str) -> RuntimeHandle:
        handle = self._handles.get(runtime_id)
        if handle is None:
            raise RuntimeErrorCode(UNSUPPORTED_RUNTIME, f"unknown runtime: {runtime_id}")
        return handle

    def handle(self, runtime_id: str) -> Optional[RuntimeHandle]:
        return self._handles.get(runtime_id)
