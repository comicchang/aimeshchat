"""Abstract base runner — contract for all backend executors."""
from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from codeagent.domain import RunRequest, RunResult


@dataclass
class RunnerConfig:
    """Shared runner configuration."""

    binary: str = ""
    timeout: int = 600
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
        cmd = self._build_cmd(request)
        if not cmd:
            return RunResult(returncode=1, stderr="failed to build command")

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.timeout,
                cwd=request.workdir or None,
            )
        except FileNotFoundError as exc:
            return RunResult(returncode=127, stderr=str(exc))
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                returncode=-1,
                stdout=exc.stdout or "",
                stderr=exc.stderr or f"timeout after {self.config.timeout}s",
            )
        except OSError as exc:
            return RunResult(returncode=1, stderr=str(exc))

        return self._parse_output(proc, request)

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    @abstractmethod
    def _build_cmd(self, request: RunRequest) -> list[str]:
        """Return the argv list for subprocess.run.

        Return an empty list to signal a build error (e.g. missing binary).
        """

    @abstractmethod
    def _parse_output(
        self, proc: subprocess.CompletedProcess[str], request: RunRequest
    ) -> RunResult:
        """Extract structured fields from process output."""
