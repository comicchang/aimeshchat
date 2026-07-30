"""Path helpers — expansion, normalization, XDG-compliant directory resolution."""
from __future__ import annotations

import os
from pathlib import Path


def expand_path(raw: str) -> str:
    """Expand ``~`` and environment variables in *raw*.

    >>> expand_path("$HOME/projects")
    '/home/user/projects'
    >>> expand_path("~/src")
    '/home/user/src'
    """
    return os.path.expanduser(os.path.expandvars(raw))


def normalize_workdir(path: str) -> str:
    """Resolve *path* to an absolute, canonical form.

    If *path* is empty or ``"."``, the current working directory is used.
    ``~`` and ``$VAR`` tokens are expanded first.

    >>> normalize_workdir("~/code")
    '/home/user/code'
    >>> normalize_workdir("")
    '/current/working/dir'
    """
    if not path or path == ".":
        path = os.getcwd()
    expanded = expand_path(path)
    return os.path.normpath(os.path.abspath(expanded))


def _xdg_dir(env_var: str, default_suffix: str) -> Path:
    """Resolve an XDG base directory.

    *env_var* is checked first; *default_suffix* is appended to ``$HOME``
    when the variable is unset.
    """
    value = os.environ.get(env_var)
    if value:
        return Path(value)
    return Path.home() / default_suffix


def config_dir() -> Path:
    """Return the codeagent configuration directory.

    ``$XDG_CONFIG_HOME/codeagent`` or ``~/.config/codeagent``.
    """
    return _xdg_dir("XDG_CONFIG_HOME", ".config") / "codeagent"


def state_dir() -> Path:
    """Return the codeagent state directory.

    ``$XDG_STATE_HOME/codeagent`` or ``~/.local/state/codeagent``.
    """
    return _xdg_dir("XDG_STATE_HOME", ".local/state") / "codeagent"


def runtime_dir() -> Path:
    """Return the codeagent runtime directory for ephemeral files.

    ``$XDG_RUNTIME_DIR/codeagent`` if set, otherwise
    ``$TMPDIR/codeagent-$UID`` (or ``/tmp/codeagent-$UID`` as a last resort).
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "codeagent"
    tmpdir = os.environ.get("TMPDIR", "/tmp")
    uid = os.getuid()
    return Path(tmpdir) / f"codeagent-{uid}"
