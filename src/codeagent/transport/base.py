"""Abstract base transport — remote execution channel."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from codeagent.constants import DEFAULT_MAILBOX_TIMEOUT
from codeagent.domain import HostSpec, RunRequest, RunResult


class Transport(ABC):
    """Execution transport — local and remote.

    LocalTransport spawns remote_exec helper locally (wire protocol).
    SSHTransport/RelayTransport spawn remote_exec on remote hosts.

    Lifecycle:
        warm(host) → pre-establish connection (idempotent)
        check(host) → verify connection alive
        execute(request, host, workdir, session_id) → run a task
        stop(host) → tear down connection
    """

    @abstractmethod
    def warm(self, host: HostSpec) -> None:
        """Pre-establish connection. Idempotent."""

    @abstractmethod
    def check(self, host: HostSpec) -> bool:
        """Return True if connection is alive."""

    @abstractmethod
    def stop(self, host: HostSpec) -> None:
        """Tear down connection. Idempotent."""

    @abstractmethod
    def execute(
        self,
        request: RunRequest,
        host: HostSpec,
        workdir: str,
        session_id: Optional[str] = None,
    ) -> RunResult:
        """Execute request on remote host. Returns result with session_id."""

    def mailbox(
        self,
        host: HostSpec,
        args: list[str],
        mailbox_root: str = "",
        timeout: int = DEFAULT_MAILBOX_TIMEOUT,
    ) -> tuple[int, str, str]:
        """Run a mailbox wire request on *host*.

        Returns ``(exit_code, stdout, stderr)``.
        Subclasses that support mailbox override this.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support mailbox operations"
        )


class TransportError(Exception):
    """Raised when a transport operation fails."""
