"""OMPRunner — wraps the local ``omp`` CLI.

Invocation::

    omp --print --mode json --cwd <workdir> [--model <model>] [--auto-approve] @<prompt-file>
    omp --print --mode json --cwd <workdir> --resume <session_id> [--model <model>] [--auto-approve] @<prompt-file>

IMPORTANT: omp does NOT read from stdin pipe.  Must use ``@<temp-file>`` for the
prompt.  Temp file must be mode 0600, deleted after use.

OMP JSONL output lines::

    {"type": "session", "id": "..."}           — session started
    {"type": "assistant", "message_end": {"message": "..."}} — final response
    {"type": "agent_end"}                      — done
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from codeagent.domain import RunRequest, RunResult

from .base import BaseRunner, RunnerConfig

_DEFAULT_BINARY = "omp"


class OMPRunner(BaseRunner):
    """Runs tasks through the local ``omp`` CLI."""

    def __init__(self, config: Optional[RunnerConfig] = None) -> None:
        super().__init__(config)
        if not self.config.binary:
            self.config.binary = _DEFAULT_BINARY

    # ------------------------------------------------------------------
    # BaseRunner contract
    # ------------------------------------------------------------------

    def _build_cmd(self, request: RunRequest) -> list[str]:
        cmd = [self.config.binary]

        # Non-interactive + JSON output
        cmd.extend(["--print", "--mode", "json"])

        if request.workdir:
            cmd.extend(["--cwd", request.workdir])

        # Resume existing session — use resume_session_id (backend ID), not session_key (namespace)
        if request.resume_session_id and not request.new_session:
            cmd.extend(["--resume", request.resume_session_id])

        # Model
        if request.model:
            cmd.extend(["--model", request.model])

        # Auto-approve (skip interactive confirmations)
        if request.skip_permissions:
            cmd.append("--auto-approve")

        # Write prompt to temp file (omp rejects stdin pipe)
        self._prompt_file = self._write_prompt_file(request.task)
        cmd.append(f"@{self._prompt_file}")

        return cmd

    def _parse_output(
        self, proc: subprocess.CompletedProcess[str], request: RunRequest
    ) -> RunResult:
        result = RunResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

        # Clean up prompt file
        self._cleanup_prompt_file()

        if proc.returncode != 0:
            return result

        # Parse JSONL output
        session_id: Optional[str] = None
        final_message: Optional[str] = None

        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "session":
                session_id = msg.get("id")

            elif msg_type == "assistant":
                end = msg.get("message_end", {})
                text = end.get("message")
                if text:
                    final_message = text

            elif msg_type == "agent_end":
                # Terminal signal — everything after is noise
                break

        result.session_id = session_id
        if final_message is not None:
            result.stdout = final_message

        return result

    # ------------------------------------------------------------------
    # Prompt temp file (mode 0600, deleted after use)
    # ------------------------------------------------------------------

    @staticmethod
    def _write_prompt_file(task: str) -> str:
        """Write the task prompt to a secure temp file and return its path."""
        fd, path = tempfile.mkstemp(prefix="omp_prompt_", suffix=".txt")
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            os.write(fd, task.encode("utf-8"))
        finally:
            os.close(fd)
        return path

    def _cleanup_prompt_file(self) -> None:
        """Remove the prompt temp file if it was created."""
        path = getattr(self, "_prompt_file", None)
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
            self._prompt_file = None

    def _cleanup(self) -> None:
        """Clean up prompt temp file."""
        self._cleanup_prompt_file()
