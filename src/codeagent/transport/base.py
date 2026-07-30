"""Abstract base transport for local and remote execution."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeagent.domain import HostSpec, RunRequest, RunResult


class Transport(ABC):
    """Base class for execution transports.

    Lifecycle:
        warm(host) → pre-establish connection (idempotent)
        check(host) → verify connection alive
        execute(request, host, workdir) → run a task
        stop(host) → tear down connection

    Implementations MUST be safe to call warm/stop multiple times.
    """

    @abstractmethod
    def warm(self, host: HostSpec) -> None:
        """Pre-establish the connection to *host*.

        Idempotent — no-op if already warm.
        Raises ``TransportError`` on failure.
        """

    @abstractmethod
    def check(self, host: HostSpec) -> bool:
        """Return True if the connection to *host* is alive."""

    @abstractmethod
    def stop(self, host: HostSpec) -> None:
        """Tear down the connection to *host*.

        Idempotent — no-op if already stopped.
        """

    @abstractmethod
    def execute(self, request: RunRequest, host: HostSpec, workdir: str) -> RunResult:
        """Execute *request* on *host* in *workdir* and return the result."""


class TransportError(Exception):
    """Raised when a transport operation fails."""
