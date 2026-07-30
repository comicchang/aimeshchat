"""Session key computation.

Key format: "{host}:{normalized_workdir}:{backend}:{agent_or_profile}"

- host: SSH alias from Target.host.ssh_alias, or LOCAL_HOST_MARKER ("__local__")
- workdir: expanded, normalized absolute path
- backend: one of codex/claude/gemini/opencode/omp (defaults to "opencode")
- agent: agent preset name, or "" if none

This key is a **NAMESPACE key** for registry lookup — it identifies the session
namespace (same host+workdir+backend+agent).  It is NOT the backend session ID.
The actual backend session ID is stored in SessionRecord.session_id after the
runner captures it.

Model is intentionally excluded: upgrading a model within the same session
namespace is a normal operation and should not create a new session.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from codeagent.domain import LOCAL_HOST_MARKER

if TYPE_CHECKING:
    from codeagent.domain import RunRequest, Target


def _normalize_workdir(path: str) -> str:
    """Expand ~ and env vars, resolve to absolute, normalize slashes."""
    if not path:
        path = os.getcwd()
    expanded = os.path.expanduser(os.path.expandvars(path))
    return os.path.normpath(os.path.abspath(expanded))


def compute_session_key(request: RunRequest, target: Target) -> str:
    """Derive the deterministic session key from a run request and its routing target.

    The key is stable across invocations with the same (host, workdir, backend, agent)
    tuple.  Model changes do NOT alter the key.
    """
    host = target.ssh_alias if not target.is_local else LOCAL_HOST_MARKER
    workdir = _normalize_workdir(target.workdir or request.workdir)
    backend = (request.backend or "opencode").lower()
    agent = request.agent or ""
    return f"{host}:{workdir}:{backend}:{agent}"
