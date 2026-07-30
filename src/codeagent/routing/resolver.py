"""Routing resolver — maps a RunRequest + RepoMap to a concrete Target."""
from __future__ import annotations

from typing import Optional

from codeagent.domain import (
    HostSpec,
    LOCAL_HOST_MARKER,
    RepoEntry,
    RepoMap,
    RunRequest,
    Target,
    TopicSpec,
    resolve_is_local,
)


# Synthetic HostSpec for local execution when no host is specified in the map.
_LOCAL_HOST = HostSpec(
    name=LOCAL_HOST_MARKER,
    ssh_alias=LOCAL_HOST_MARKER,
    hostnames=("localhost",),
    description="local fallback",
    transport="local",
)


def resolve_target(request: RunRequest, repo_map: RepoMap) -> Target:
    """Resolve *request* against *repo_map* and return a concrete :class:`Target`.

    Resolution priority:

    1. ``request.host`` is set → find (or synthesize) a :class:`HostSpec`,
       use ``request.workdir`` as the repo path.
    2. ``request.topic`` is set → look up :class:`TopicSpec`, pick the repo at
       ``request.repo_index``, resolve its host from the map.
    3. Neither → run locally in ``request.workdir`` (or cwd).

    In every case :func:`resolve_is_local` is consulted to set
    ``Target.is_local``.
    """
    # --- path 1: explicit host ---
    if request.host:
        return _resolve_by_host(request, repo_map)

    # --- path 2: topic-based routing ---
    if request.topic:
        return _resolve_by_topic(request, repo_map)

    # --- path 3: local fallback ---
    return _resolve_local_fallback(request)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_by_host(request: RunRequest, repo_map: RepoMap) -> Target:
    """Route to an explicitly-requested host."""
    host = repo_map.hosts.get(request.host)
    if host is None:
        # Ad-hoc host: not in the map — synthesize a minimal HostSpec.
        host = HostSpec(
            name=request.host,
            ssh_alias=request.host,
            hostnames=(request.host,),
            description="ad-hoc host",
        )
    is_local = resolve_is_local(host)
    workdir = request.workdir or "."
    repo = RepoEntry(host=host.name, path=workdir)
    return Target(host=host, repo=repo, is_local=is_local)


def _resolve_by_topic(request: RunRequest, repo_map: RepoMap) -> Target:
    """Route via topic → repo_index → host lookup."""
    topic = repo_map.topic(request.topic)  # raises KeyError if missing
    repo_entry = topic.repo(request.repo_index)  # raises IndexError if OOB
    host = repo_map.hosts.get(repo_entry.host)
    if host is None:
        host = HostSpec(
            name=repo_entry.host,
            ssh_alias=repo_entry.host,
            hostnames=(repo_entry.host,),
            description="unmapped topic host",
        )
    is_local = resolve_is_local(host)
    return Target(
        host=host,
        repo=repo_entry,
        topic=topic,
        repo_index=request.repo_index,
        is_local=is_local,
    )


def _resolve_local_fallback(request: RunRequest) -> Target:
    """Run locally — no host or topic specified."""
    workdir = request.workdir or "."
    repo = RepoEntry(host=_LOCAL_HOST.name, path=workdir)
    return Target(host=_LOCAL_HOST, repo=repo, is_local=True)
