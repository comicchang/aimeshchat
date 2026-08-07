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
import logging
import os
import secrets
import stat
import subprocess
import tempfile
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from codeagent.domain import RunRequest, RunResult
from codeagent.hooks.swarm_hooks import on_agent_start, on_agent_stop

from .base import BaseRunner, RunnerConfig

LOG = logging.getLogger(__name__)

_DEFAULT_BINARY = "omp"

# Oracle-class agents (park=true, auto-exit=false) get a 1-hour timeout
# instead of the default 10-minute DEFAULT_EXEC_TIMEOUT.
_ORACLE_TIMEOUT = 3600


# ---------------------------------------------------------------------------
# Agent profile resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentProfile:
    """Resolved agent profile from ``~/.omp/agent/agents/<name>.md`` frontmatter."""

    name: str
    model: str = ""
    thinking: str = ""
    system_prompt_path: str = ""  # path to the .md file for --append-system-prompt
    auto_exit: bool = True
    park: bool = False
    park_class: str = ""
    spawning: bool = False


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse simple YAML frontmatter (key: value pairs) between ``---`` markers."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm_text = text[4:end]  # skip opening "---\n"
    result: dict[str, str] = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            # Strip matching quotes
            if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            result[key] = value
    return result


def resolve_agent_profile(agent_name: str) -> AgentProfile:
    """Load agent profile from ``~/.omp/agent/agents/<name>.md`` frontmatter.

    Raises :class:`ValueError` if the profile file does not exist.
    """
    profile_path = Path.home() / ".omp" / "agent" / "agents" / f"{agent_name}.md"
    if not profile_path.exists():
        raise ValueError(f"Unknown agent profile: {agent_name}")

    text = profile_path.read_text(encoding="utf-8")
    fm = _parse_frontmatter(text)

    return AgentProfile(
        name=fm.get("name", agent_name),
        model=fm.get("model", ""),
        thinking=fm.get("thinking", ""),
        system_prompt_path=str(profile_path),
        auto_exit=fm.get("auto-exit", "true").lower() == "true",
        park=fm.get("park", "false").lower() == "true",
        park_class=fm.get("park-class", ""),
        spawning=fm.get("spawning", "false").lower() == "true",
    )


class OMPRunner(BaseRunner):
    """Runs tasks through the local ``omp`` CLI."""

    def __init__(self, config: Optional[RunnerConfig] = None) -> None:
        super().__init__(config)
        if not self.config.binary:
            self.config.binary = _DEFAULT_BINARY
        self._identity_path: Optional[Path] = None

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

        # ── Agent profile resolution ──────────────────────────────────
        # If request.agent is set, load profile from ~/.omp/agent/agents/<agent>.md
        # and apply model / thinking / system-prompt from frontmatter.
        # Explicit request.model still wins over profile model.
        profile = getattr(self, "_agent_profile", None)

        if profile:
            # Thinking level from profile
            if profile.thinking:
                cmd.extend(["--thinking", profile.thinking])
            # System prompt from agent profile (--append-system-prompt takes a file path)
            if profile.system_prompt_path:
                cmd.extend(["--append-system-prompt", profile.system_prompt_path])

        # Model: explicit request.model > profile model
        model = request.model or (profile.model if profile else None)
        if model:
            cmd.extend(["--model", model])

        # Auto-approve (skip interactive confirmations)
        if request.skip_permissions:
            cmd.append("--auto-approve")

        # Write prompt to temp file (omp rejects stdin pipe)
        self._prompt_file = self._write_prompt_file(request.task)
        cmd.append(f"@{self._prompt_file}")

        return cmd

    def run(self, request: RunRequest) -> RunResult:
        """Execute with agent-profile timeout override and swarm session bootstrap.

        Oracle-class agents (park=true, auto-exit=false) get a 1-hour timeout
        instead of the default 10-minute ``DEFAULT_EXEC_TIMEOUT``.
        """
        # Resolve agent profile early for timeout + swarm session
        self._agent_profile: Optional[AgentProfile] = None
        if request.agent:
            try:
                self._agent_profile = resolve_agent_profile(request.agent)
            except ValueError as exc:
                return RunResult(returncode=1, stderr=str(exc))

        # Override timeout for oracle-class agents
        original_timeout: Optional[int] = None
        if self._agent_profile and self._agent_profile.park and not self._agent_profile.auto_exit:
            original_timeout = self.config.timeout
            self.config.timeout = _ORACLE_TIMEOUT
            LOG.debug(
                "agent=%s is oracle-class (park=true, auto-exit=false); "
                "overriding timeout %ds → %ds",
                request.agent, original_timeout, _ORACLE_TIMEOUT,
            )

        # Ensure swarm session for oracle agents so _extra_env activates
        if self._agent_profile and self._agent_profile.park:
            self._ensure_swarm_session(request)

        try:
            return super().run(request)
        finally:
            if original_timeout is not None:
                self.config.timeout = original_timeout

    def _ensure_swarm_session(self, request: RunRequest) -> None:
        """Create a swarm session for oracle agents if ``SWARM_SESSION_ID`` is not set.

        The ``_extra_env`` method only injects mailbox identity when
        ``SWARM_SESSION_ID`` (or ``OMP_SESSION_ID``) is present in the
        environment.  For oracle agents spawned from ``_cmd_run`` / ``_execute``
        — which never create a swarm session — this method bridges the gap by
        generating a session ID and registering manager ↔ agent in the swarm
        kernel.
        """
        swarm_sid = os.environ.get("SWARM_SESSION_ID") or os.environ.get("OMP_SESSION_ID")
        if swarm_sid:
            return  # already wired

        # Generate a new swarm session ID
        swarm_sid = f"swarm_{int(_time.time())}_{secrets.token_hex(4)}"
        os.environ["SWARM_SESSION_ID"] = swarm_sid

        # Set MAILBOX_ROOT if not already set (required by _extra_env)
        if not os.environ.get("MAILBOX_ROOT"):
            mailbox_root = Path.home() / ".omp" / "mailbox"
            mailbox_root.mkdir(parents=True, exist_ok=True)
            os.environ["MAILBOX_ROOT"] = str(mailbox_root)

        # Set OMP_WORKER_ID for the agent
        agent_name = request.agent or "agent"
        if not os.environ.get("OMP_WORKER_ID"):
            os.environ["OMP_WORKER_ID"] = agent_name

        # Create session in swarm kernel (manager ↔ agent roster)
        try:
            from codeagent.hooks.swarm_hooks import _get_kernel
            kernel, _store = _get_kernel()
            manager_id = "manager"
            kernel.create_session(
                session_id=swarm_sid,
                manager_id=manager_id,
                roster=[manager_id, agent_name],
            )
            LOG.info(
                "created swarm session %s for agent=%s (roster: %s ↔ %s)",
                swarm_sid, agent_name, manager_id, agent_name,
            )
        except Exception:
            LOG.warning(
                "failed to create swarm session for agent=%s; "
                "mailbox will be unavailable",
                request.agent, exc_info=True,
            )

    def _extra_env(self) -> Optional[dict[str, str]]:
        """Inject swarm mailbox identity for the OMP plugin (Oracle P1-3).

        The plugin reads OMP_MAILBOX_SESSION_ID / OMP_MAILBOX_AGENT_ID at
        startup from the OS env (inherited through the launcher). Identity
        belongs to the launcher, NOT to the agent's reasoning — inject it
        here so the plugin activates without the agent hand-writing an
        identity file.

        Env vars (checked in precedence order):
            SWARM_SESSION_ID  — canonical; set by swarm dispatcher.
            OMP_SESSION_ID    — alias; used when SWARM_SESSION_ID is absent.
        """

        # Accept both SWARM_SESSION_ID (canonical) and OMP_SESSION_ID (alias).
        # SWARM_SESSION_ID takes precedence when both are set.
        swarm_sid = os.environ.get("SWARM_SESSION_ID") or os.environ.get("OMP_SESSION_ID")
        if not swarm_sid:
            return None

        token = f"{int(_time.time())}_{secrets.token_hex(4)}"
        identity_dir = Path.home() / ".omp" / "mailbox-identity"
        identity_dir.mkdir(parents=True, exist_ok=True)
        identity_path = identity_dir / f"{token}.json"
        # Oracle 验证：identity 的 worker_id 必须非空——插件 readIdentityFile 拒绝
        # 空 worker_id，activate 永不进入（现场 64/64 identity 曾为空）。
        worker_id = os.environ.get("OMP_WORKER_ID", "")
        if not worker_id:
            worker_id = "worker"  # 缺省非空（调用方应显式设 OMP_WORKER_ID）
        # Backward-compat identity file for plugins that still read it
        nonce = secrets.token_hex(8)
        identity_path.write_text(json.dumps({
            "session_id": swarm_sid,
            "worker_id": worker_id,
            "owner_pid": os.getpid(),
            "nonce": nonce,
        }))
        self._identity_path = identity_path

        return {
            "SWARM_SESSION_ID": swarm_sid,
            "OMP_MAILBOX_SESSION_ID": swarm_sid,
            "OMP_MAILBOX_AGENT_ID": worker_id,
            "OMP_MAILBOX_IDENTITY_FILE": str(identity_path),
            "OMP_MAILBOX_NONCE": nonce,
            "MAILBOX_ROOT": os.environ.get("MAILBOX_ROOT", ""),
        }

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

        # ── Swarm hook: register agent on session start ────────────────
        # When SWARM_SESSION_ID is set, register this agent in the swarm
        # kernel so other agents can discover and message it.
        swarm_sid = os.environ.get("SWARM_SESSION_ID")
        if swarm_sid and session_id:
            try:
                on_agent_start(
                    session_id=swarm_sid,
                    agent_id=session_id,
                    host_alias="__local__",
                    backend="omp",
                )
                self._swarm_session_id = swarm_sid
                self._swarm_agent_id = session_id
            except Exception:
                LOG.warning("on_agent_start hook failed", exc_info=True)

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
        """Clean up prompt temp file, identity file, and unregister swarm agent."""
        # ── Park lifecycle guard: skip cleanup if agent is HOT_PARKED ──
        try:
            from codeagent.park.registry import ParkRegistry
            from codeagent.domain.park import Lifecycle
            pr = ParkRegistry()
            swarm_aid = getattr(self, "_swarm_agent_id", None)
            if swarm_aid:
                manifest = pr.lookup_by_field("backend_session_id", swarm_aid)
                if manifest is not None and manifest.lifecycle == Lifecycle.HOT_PARKED:
                    LOG.info("agent %s is HOT_PARKED, skipping identity cleanup", swarm_aid)
                    return  # keep identity for revive
        except Exception:
            pass  # park module unavailable — fall through to normal cleanup

        self._cleanup_prompt_file()
        # Remove the injected mailbox identity file (per-run, unique token)
        identity_path = getattr(self, "_identity_path", None)
        if identity_path:
            try:
                identity_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._identity_path = None
        # ── Swarm hook: unregister agent on stop ──────────────────────
        swarm_sid = getattr(self, "_swarm_session_id", None)
        swarm_aid = getattr(self, "_swarm_agent_id", None)
        if swarm_sid and swarm_aid:
            try:
                on_agent_stop(session_id=swarm_sid, agent_id=swarm_aid)
            except Exception:
                LOG.warning("on_agent_stop hook failed", exc_info=True)
