"""Abstract base runner — contract for all backend executors."""
from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from codeagent.constants import DEFAULT_EXEC_TIMEOUT
from codeagent.domain import RunRequest, RunResult


@dataclass
class RunnerConfig:
    """Shared runner configuration."""

    binary: str = ""
    timeout: int = DEFAULT_EXEC_TIMEOUT
    output_dir: Optional[Path] = None


class BaseRunner(ABC):
    """Abstract runner — subclasses implement ``_build_cmd`` and ``_parse_output``.

    The base class owns the subprocess lifecycle:
      1. Build the command via ``_build_cmd(request)``.
      2. Run it with ``subprocess.run`` (Popen under the hood for timeout).
      3. Parse structured output via ``_parse_output(proc, request)``.
      4. Return a ``RunResult``.
    """

    def __init__(self, config: Optional[RunnerConfig] = None) -> None:
        self.config = config or RunnerConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, request: RunRequest) -> RunResult:
        """Execute a task and return the result."""
        import signal

        cmd = self._build_cmd(request)
        if not cmd:
            return RunResult(returncode=1, stderr="failed to build command")

        proc = None
        try:
            env = None
            extra_env = self._extra_env()
            if extra_env:
                env = os.environ.copy()
                env.update(extra_env)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=request.workdir or None,
                start_new_session=True,
                env=env,
            )
            stdout, stderr = proc.communicate(
                input=request.task,
                timeout=self.config.timeout,
            )
            proc_obj = subprocess.CompletedProcess(
                cmd, proc.returncode, stdout, stderr
            )
        except subprocess.TimeoutExpired:
            if proc is not None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            self._cleanup()
            return RunResult(
                returncode=-1,
                stderr=f"timeout after {self.config.timeout}s",
            )
        except FileNotFoundError as exc:
            self._cleanup()
            return RunResult(returncode=127, stderr=str(exc))
        except OSError as exc:
            self._cleanup()
            return RunResult(returncode=1, stderr=str(exc))

        return self._parse_output(proc_obj, request)

    def _cleanup(self) -> None:
        """Clean up temporary resources. Subclasses should override."""
        pass

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    @abstractmethod
    def _build_cmd(self, request: RunRequest) -> list[str]:
        """Return the argv list for subprocess.run.

        Return an empty list to signal a build error (e.g. missing binary).
        """

    def _extra_env(self) -> Optional[dict[str, str]]:
        """Extra environment variables for the subprocess, or None.

        Subclasses override to inject identity/context (e.g. OMP mailbox
        identity for the swarm plugin). Called only when non-empty.
        """
        return None

    @abstractmethod
    def _parse_output(
        self, proc: subprocess.CompletedProcess[str], request: RunRequest
    ) -> RunResult:
        """Extract structured fields from process output."""
