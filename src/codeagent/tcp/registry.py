"""In-memory connection registry and session routing for the TCP daemon.

Both classes are plain dict-backed stores — safe under asyncio's
cooperative concurrency model (no asyncio.Lock needed for reads/writes
since only the event loop thread mutates state).
"""
from __future__ import annotations

import asyncio
from typing import Optional


class ConnectionRegistry:
    """Track live TCP connections by ``host_alias``.

    Each entry maps a host alias to its ``(StreamReader, StreamWriter)``
    pair.  The registry is the single source of truth for which remote
    hosts are currently reachable over the daemon's TCP listener.
    """

    def __init__(self) -> None:
        self._connections: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}

    # ── mutation ────────────────────────────────────────────────────────

    def register(
        self,
        host_alias: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Register (or replace) a connection for *host_alias*."""
        self._connections[host_alias] = (reader, writer)

    def remove(self, host_alias: str) -> None:
        """Remove the connection entry for *host_alias* (no-op if absent)."""
        self._connections.pop(host_alias, None)

    # ── lookup ──────────────────────────────────────────────────────────

    def get(
        self, host_alias: str
    ) -> Optional[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        """Return ``(reader, writer)`` for *host_alias*, or ``None``."""
        return self._connections.get(host_alias)

    def list_hosts(self) -> list[str]:
        """Return a snapshot of all currently registered host aliases."""
        return list(self._connections.keys())

    def is_connected(self, host_alias: str) -> bool:
        """``True`` if *host_alias* has a registered connection."""
        return host_alias in self._connections

    def clear(self) -> None:
        """Remove all entries."""
        self._connections.clear()


class SessionRoutingTable:
    """Map session IDs to the set of host aliases that participate.

    Used by the daemon to decide which connected hosts should receive
    a forwarded message for a given session.
    """

    def __init__(self) -> None:
        self._routes: dict[str, set[str]] = {}

    # ── mutation ────────────────────────────────────────────────────────

    def add_route(self, session_id: str, host_alias: str) -> None:
        """Ensure *host_alias* is listed for *session_id*."""
        self._routes.setdefault(session_id, set()).add(host_alias)

    def remove_route(self, session_id: str, host_alias: str) -> None:
        """Remove *host_alias* from *session_id*'s host set.

        Cleans up the session key entirely when the set becomes empty.
        """
        hosts = self._routes.get(session_id)
        if hosts is None:
            return
        hosts.discard(host_alias)
        if not hosts:
            del self._routes[session_id]

    # ── lookup ──────────────────────────────────────────────────────────

    def get_hosts(self, session_id: str) -> set[str]:
        """Return the host set for *session_id* (empty set if unknown)."""
        return set(self._routes.get(session_id, set()))

    def get_all_sessions(self) -> dict[str, set[str]]:
        """Return a shallow copy of the full routing table."""
        return {sid: set(hosts) for sid, hosts in self._routes.items()}

    def clear(self) -> None:
        """Remove all routes."""
        self._routes.clear()
