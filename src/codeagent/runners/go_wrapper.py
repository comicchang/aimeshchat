"""GoWrapperRunner — wraps the existing Go codeagent-wrapper binary.

Binary location: ``~/.claude/bin/codeagent-wrapper``

Invocation::

    codeagent-wrapper [flags] <task> [workdir]
    codeagent-wrapper resume <session_id> <task> [workdir]

Key flags: --backend, --agent, --model, --skills, --skip-permissions, --output <json-file>

stderr contains:
    SESSION_ID: <id>
    Selected backend: <name>

--output <file> writes JSON: {session_id, task_id, pid, backend, status, error}
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from codeagent.domain import RunRequest, RunResult

from .base import BaseRunner, RunnerConfig

_DEFAULT_BINARY = os.path.expanduser("~/.claude/bin/codeagent-wrapper")

_SESSION_ID_RE = re.compile(r"^SESSION_ID:\s*(\S+)", re.MULTILINE)
_BACKEND_RE = re.compile(r"^Selected backend:\s*(\S+)", re.MULTILINE)


class GoWrapperRunner(BaseRunner):
    """Runs tasks through the Go ``codeagent-wrapper`` binary."""

    def __init__(self, config: Optional[RunnerConfig] = None) -> None:
        super().__init__(config)
        if not self.config.binary:
            env_bin = os.environ.get("CODEAGENT_WRAPPER_BIN")
            if env_bin:
                self.config.binary = env_bin
            else:
                self.config.binary = shutil.which("codeagent-wrapper") or _DEFAULT_BINARY

    # ------------------------------------------------------------------
    # BaseRunner contract
    # ------------------------------------------------------------------

    def _build_cmd(self, request: RunRequest) -> list[str]:
        cmd = [self.config.binary]

        # Resume path — use resume_session_id (backend ID), not session_key (namespace)
        if request.resume_session_id and not request.new_session:
            cmd.append("resume")
            cmd.append(request.resume_session_id)
        elif request.resume_session_id and request.new_session:
            # --new-session overrides resume — just pass task normally
            pass

        # Flags (only when non-default)
        if request.backend:
            cmd.extend(["--backend", request.backend])
        if request.agent:
            cmd.extend(["--agent", request.agent])
        if request.model:
            cmd.extend(["--model", request.model])
        if request.skills:
            cmd.extend(["--skills", request.skills])
        if request.skip_permissions:
            cmd.append("--skip-permissions")

        # Output file for structured JSON (optional but recommended)
        self._output_file: Optional[Path] = None
        if self.config.output_dir:
            self._output_file = self.config.output_dir / f"go_out_{os.getpid()}.json"
            cmd.extend(["--output", str(self._output_file)])
        else:
            tmp = tempfile.NamedTemporaryFile(
                prefix="codeagent_go_", suffix=".json", delete=False
            )
            self._output_file = Path(tmp.name)
            tmp.close()
            cmd.extend(["--output", str(self._output_file)])

        # Positional args: <task> [workdir]
        cmd.append(request.task)
        if request.workdir:
            cmd.append(request.workdir)

        return cmd

    def _parse_output(
        self, proc: subprocess.CompletedProcess[str], request: RunRequest
    ) -> RunResult:
        result = RunResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

        # Try structured JSON output first
        if self._output_file and self._output_file.exists():
            try:
                data = json.loads(self._output_file.read_text())
                result.session_id = data.get("session_id") or None
                result.backend = data.get("backend", "")
                if data.get("status") == "error" and data.get("error"):
                    result.stderr = data["error"]
            except (json.JSONDecodeError, OSError):
                pass
            finally:
                try:
                    self._output_file.unlink(missing_ok=True)
                except OSError:
                    pass

        # Fallback: parse stderr for session / backend
        if not result.session_id:
            m = _SESSION_ID_RE.search(proc.stderr)
            if m:
                result.session_id = m.group(1)

        if not result.backend:
            m = _BACKEND_RE.search(proc.stderr)
            if m:
                result.backend = m.group(1)

        return result

    def _cleanup(self) -> None:
        """Clean up temp output file."""
        output_file = getattr(self, "_output_file", None)
        if output_file:
            try:
                output_file.unlink(missing_ok=True)
            except OSError:
                pass



