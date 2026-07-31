"""repo-map.json config loader.

Parses the global repo-map.json and topic-level .repo-map.json files,
returning a fully-validated RepoMap using domain types.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

from codeagent.domain import HostSpec, RepoEntry, RepoMap, TopicSpec
from codeagent.util.paths import expand_path

VALID_TRANSPORTS: Final[frozenset[str]] = frozenset({"ssh", "relay-login"})


def _default_repo_map_path() -> Path:
    """Find repo-map.json: env var > XDG config > ~/.codeagent > dotai profiles."""
    env = os.environ.get("CODEAGENT_REPO_MAP")
    if env:
        return Path(env)
    candidates = [
        Path.home() / ".config" / "codeagent" / "repo-map.json",
        Path.home() / ".codeagent" / "repo-map.json",
        Path.home() / "src" / "dotai" / "profiles" / "policy" / "repo-map.json",
        Path.home() / "projects" / "config" / "repo-map.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "repo-map.json not found. Set CODEAGENT_REPO_MAP or create ~/.config/codeagent/repo-map.json"
    )


def load_repo_map(path: Path | str | None = None) -> RepoMap:
    """Parse a global repo-map.json and scan for topic-level configs.

    1. Read the global JSON → extract midocs_root + hosts + relay_zsh.
    2. Expand ~ and env vars in midocs_root / relay_zsh / paths.
    3. Glob midocs_root/*/.repo-map.json (skip dot-prefixed dirs).
    4. Each topic directory name is the topic name; parse its .repo-map.json.
    5. If any repo references an undefined host → raise ValueError.
    6. If midocs_root doesn't exist or has no topics → return empty topics (no error).
    """
    if path is None:
        path = _default_repo_map_path()
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))

    midocs_root = Path(expand_path(raw.get("midocs_root", "~/mi-docs")))
    relay_zsh = expand_path(raw.get("relay_zsh", ""))

    hosts: dict[str, HostSpec] = {}
    for name, cfg in (raw.get("hosts") or {}).items():
        transport = cfg.get("transport", "ssh")
        if transport not in VALID_TRANSPORTS:
            raise ValueError(
                f"host '{name}' transport invalid: {transport}, "
                f"must be one of {', '.join(sorted(VALID_TRANSPORTS))}"
            )
        ssh_alias = cfg.get("ssh_alias", "")
        if not isinstance(ssh_alias, str) or not ssh_alias.strip():
            raise ValueError(
                f"host '{name}' ssh_alias must be a non-empty string, got: {ssh_alias!r}"
            )
        raw_fallback = cfg.get("fallback_ssh_alias")
        if raw_fallback is None or raw_fallback == "":
            fallback = ""
        elif not isinstance(raw_fallback, str) or not raw_fallback.strip():
            raise ValueError(
                f"host '{name}' fallback_ssh_alias must be a non-empty string, "
                f"got: {raw_fallback!r}"
            )
        else:
            fallback = raw_fallback.strip()
            if fallback == ssh_alias:
                raise ValueError(
                    f"host '{name}' fallback_ssh_alias must differ from ssh_alias: {fallback!r}"
                )
            if transport != "ssh":
                raise ValueError(
                    f"host '{name}' transport='{transport}' does not support fallback_ssh_alias "
                    f"(only ssh does)"
                )
        hosts[name] = HostSpec(
            name=name,
            ssh_alias=ssh_alias,
            hostnames=tuple(cfg.get("hostnames") or ()),
            description=cfg.get("description", ""),
            shell_prefix=cfg.get("shell_prefix", "").strip(),
            transport=transport,
            fallback_ssh_alias=fallback,
        )

    topics: dict[str, TopicSpec] = {}

    if midocs_root.is_dir():
        for topic_dir in sorted(midocs_root.iterdir()):
            if not topic_dir.is_dir() or topic_dir.name.startswith("."):
                continue
            repo_map_file = topic_dir / ".repo-map.json"
            if not repo_map_file.is_file():
                continue

            topic_name = topic_dir.name
            topic_raw = json.loads(repo_map_file.read_text(encoding="utf-8"))

            repos: list[RepoEntry] = []
            for entry in (topic_raw.get("repos") or []):
                host_key = entry["host"]
                if host_key not in hosts:
                    raise ValueError(
                        f"topic '{topic_name}' repo references undefined host: {host_key}"
                    )
                repos.append(RepoEntry(
                    host=host_key,
                    path=entry["path"].strip(),
                    note=entry.get("note", ""),
                ))

            topics[topic_name] = TopicSpec(
                name=topic_name,
                repos=tuple(repos),
                description=topic_raw.get("description", ""),
            )

    return RepoMap(
        midocs_root=midocs_root,
        hosts=hosts,
        topics=topics,
        relay_zsh=relay_zsh,
    )
