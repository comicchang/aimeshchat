"""Centralized transport selection — single source of truth for host→transport."""
from __future__ import annotations

from typing import TYPE_CHECKING

from codeagent.transport.local import LocalTransport
from codeagent.transport.ssh import SSHTransport

if TYPE_CHECKING:
    from codeagent.domain import HostSpec, RepoMap
    from codeagent.transport.base import Transport


class TransportRouter:
    """Select the right Transport for a given HostSpec.

    Routing rules:
        transport == "relay-login"  →  RelayTransport (requires relay_zsh)
        otherwise                   →  SSHTransport (ControlMaster)

    Usage::

        router = TransportRouter()
        transport = router.get(host_spec, repo_map)
        caps = router.capabilities(host_spec)
    """

    def get(self, host: HostSpec, repo_map: RepoMap | None = None) -> Transport:
        """Return a Transport instance for *host*.

        Top2 (oracle): 本机 host（hostnames 匹配当前主机）→ LocalTransport，
        不再默认 SSH——否则 deliver/flush 会把本机当远程 SSH 到别名。
        For relay-login hosts, ``repo_map.relay_zsh`` must be set.
        """
        from codeagent.domain import resolve_is_local

        if resolve_is_local(host):
            return LocalTransport()
        if host.transport == "relay-login":
            relay_zsh = getattr(repo_map, "relay_zsh", "") if repo_map else ""
            if not relay_zsh:
                raise ValueError(
                    f"host '{host.name}' uses relay-login but relay_zsh not configured"
                )
            from codeagent.transport.relay import RelayTransport

            return RelayTransport(relay_zsh)
        return SSHTransport()

    def capabilities(self, host: HostSpec) -> set[str]:
        """Return the set of capability strings supported by *host*'s transport.

        SSH   → ``{'mailbox', 'stream', 'artifact'}``  (ControlMaster supports all)
        relay → ``{'mailbox'}``  (one-shot PTY invoke, no persistent stream)
        local → ``{'mailbox', 'stream', 'artifact'}``
        """
        if host.transport == "relay-login":
            return {"mailbox"}
        # Default (ssh) and local both support full capabilities.
        return {"mailbox", "stream", "artifact"}

    def supports_mailbox(self, host: HostSpec) -> bool:
        """Return True if *host*'s transport supports mailbox operations."""
        return "mailbox" in self.capabilities(host)

    def supports_stream(self, host: HostSpec) -> bool:
        """Return True if *host*'s transport supports long-lived streams."""
        return "stream" in self.capabilities(host)
