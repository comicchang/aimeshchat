"""Abstract base runner — contract for all backend executors.

The base class owns the subprocess lifecycle split into four primitives so
long tasks are no longer bound to ``Popen.wait`` / ``communicate(timeout)``:

  - ``spawn(request)`` — build argv + env, Popen
  - ``pump(proc)``     — yield (channel, line) reading stdout AND stderr
                         concurrently (a full stderr pipe must not deadlock)
  - ``wait(proc)``     — wait for exit, build CompletedProcess
  - ``stop(proc)``     — terminate the process group

``run()`` composes them for the legacy bounded path. Long tasks are
supervised by the gateway + tmux runtime, NOT by this runner.
"""
from __future__ import annotations

import io
import os
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from codeagent.constants import DEFAULT_EXEC_TIMEOUT
from codeagent.domain import RunRequest, RunResult


@dataclass
class RunnerConfig:
    """Shared runner configuration."""

    binary: str = ""
    timeout: int = DEFAULT_EXEC_TIMEOUT
    output_dir: Optional[Path] = None


class BaseRunner(ABC):
    """Abstract runner — subclasses implement ``_build_cmd``, ``_extra_env``,
    ``_consume_line`` and ``_parse_output``.

    Lifecycle primitives (spawn/pump/wait/stop) let callers drive long
    tasks without binding them to a shell ``wait``.
    """

    def __init__(self, config: Optional[RunnerConfig] = None) -> None:
        self.config = config or RunnerConfig()

    # ------------------------------------------------------------------
    # Lifecycle primitives
    # ------------------------------------------------------------------

    def spawn(self, request: RunRequest) -> subprocess.Popen:
        """Build argv + env and start the process. Returns the Popen."""
        cmd = self._build_cmd(request)
        if not cmd:
            raise ValueError("failed to build command")
        env = None
        extra_env = self._extra_env()
        if extra_env:
            env = os.environ.copy()
            env.update(extra_env)
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,  # P2-17: OMP readPipedInput blocks on non-TTY stdin waiting for EOF that never arrives; prompt is via @file
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=request.workdir or None,
            start_new_session=True,
            env=env,
        )

    def pump(self, proc: subprocess.Popen) -> Iterator[tuple[str, str]]:
        """Yield ``(channel, line)`` reading stdout and stderr concurrently.

        stderr is drained by a daemon thread into a buffer; stdout is read
        line-by-line on the caller thread. Concurrent draining prevents a
        full stderr pipe from deadlocking a chatty agent.
        """
        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            stream = proc.stderr
            if stream is None:
                return
            for line in stream:
                stderr_lines.append(line)

        t = threading.Thread(target=_drain_stderr, daemon=True)
        t.start()

        stdout_stream = proc.stdout
        if not hasattr(stdout_stream, "readline"):
            # Tests may hand us a plain string.
            stdout_stream = io.StringIO(stdout_stream if isinstance(stdout_stream, str) else "")
        if stdout_stream is not None:
            for line in stdout_stream:
                yield "stdout", line
        t.join(timeout=5)
        for line in stderr_lines:
            yield "stderr", line

    def wait(self, proc: subprocess.Popen, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        """Wait for exit and return a CompletedProcess (stdout from pump)."""
        timeout = timeout if timeout is not None else self.config.timeout
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise
        return subprocess.CompletedProcess(
            proc.args, proc.returncode, "", "",
        )

    def stop(self, proc: subprocess.Popen) -> None:
        """Terminate the process group (SIGKILL) — for timeouts/aborts."""
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    # ------------------------------------------------------------------
    # Public API (legacy bounded path)
    # ------------------------------------------------------------------

    def run(self, request: RunRequest) -> RunResult:
        """Execute a bounded task and return the result.

        Used only for explicit short tasks (OMPRunner ``--print``). Long
        tasks are supervised by the gateway + tmux runtime.
        """
        try:
            proc = self.spawn(request)
        except ValueError as exc:
            self._cleanup()
            return RunResult(returncode=1, stderr=str(exc))
        except FileNotFoundError as exc:
            self._cleanup()
            return RunResult(returncode=127, stderr=str(exc))
        except OSError as exc:
            self._cleanup()
            return RunResult(returncode=1, stderr=str(exc))

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        try:
            for channel, line in self.pump(proc):
                if channel == "stdout":
                    stdout_lines.append(line)
                    self._consume_line("stdout", line)
                else:
                    stderr_lines.append(line)
            proc_obj = self.wait(proc)
        except subprocess.TimeoutExpired:
            self.stop(proc)
            self._cleanup()
            return RunResult(
                returncode=-1,
                stderr=f"timeout after {self.config.timeout}s",
            )
        except OSError as exc:
            self._cleanup()
            return RunResult(returncode=1, stderr=str(exc))

        proc_obj.stdout = "".join(stdout_lines)
        proc_obj.stderr = "".join(stderr_lines)
        try:
            return self._parse_output(proc_obj, request)
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        """Clean up temporary resources. Subclasses should override."""
        pass

    def _consume_line(self, channel: str, line: str) -> None:
        """Incrementally process each output line from the subprocess.

        Called on the main thread for every line of stdout while the
        subprocess is still running.  Subclasses override to parse
        structured output (e.g. JSONL events) without waiting for exit.
        """
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
        """Extra environment variables for the subprocess, or None."""
        return None

    @abstractmethod
    def _parse_output(
        self, proc: subprocess.CompletedProcess, request: RunRequest
    ) -> RunResult:
        """Extract structured fields from process output."""
