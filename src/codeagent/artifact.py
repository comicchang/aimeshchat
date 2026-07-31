"""Artifact transport — selective file pull over SSH ControlMaster.

Worker sends artifact descriptor in REPORT/EVIDENCE:
  {artifact_id, relative_path, size, sha256, media_type}

Manager pulls via existing SSH ControlMaster (scp/sftp).
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from codeagent.constants import DEFAULT_PULL_TIMEOUT
from codeagent.transport.base import TransportError
from codeagent.transport.control_master import socket_path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArtifactDescriptor:
    """Descriptor for a single artifact on the remote host.

    Fields match the JSON sent in a REPORT/EVIDENCE message from the worker.
    """

    artifact_id: str
    relative_path: str  # relative to artifact root on the remote
    size: int
    sha256: str
    media_type: str = "application/octet-stream"


# ── path safety ───────────────────────────────────────────────────────


def validate_descriptor(desc: ArtifactDescriptor) -> None:
    """Validate that *desc.relative_path* is safe — no traversal, no absolutes.

    Raises ``ValueError`` on any safety violation.
    """
    path = desc.relative_path

    # 1. Reject empty paths.
    if not path or not path.strip():
        raise ValueError(f"empty relative_path")

    # 2. Reject absolute paths.
    if os.path.isabs(path):
        raise ValueError(f"absolute path not allowed: {path!r}")

    # 3. Normalize and reject traversal.
    normalized = os.path.normpath(path)
    if os.path.isabs(normalized):
        raise ValueError(f"normalization produced absolute path: {path!r}")

    # 4. Reject any component that is ``..`` (belt-and-suspenders).
    parts = []
    for part in normalized.split(os.sep):
        if part == "..":
            raise ValueError(f"'..' component in path: {path!r}")
        if part:
            parts.append(part)

    # 5. Must resolve to a non-empty relative path.
    if not parts:
        raise ValueError(f"path resolves to root: {path!r}")


# ── pull ──────────────────────────────────────────────────────────────


def pull_artifact(
    host_alias: str,
    remote_root: str,
    desc: ArtifactDescriptor,
    dest: Path,
    *,
    ssh_bin: str = "ssh",
    scp_bin: str = "scp",
) -> Path:
    """Pull a single artifact from *host_alias* via SSH ControlMaster.

    *remote_root* is the absolute directory on the remote where artifacts
    live (e.g. ``/tmp/codeagent-artifacts/<session>``).  *desc.relative_path*
    is resolved underneath it.

    Uses ``scp`` over the existing ControlMaster socket for zero-auth,
    zero-latency transfer.  Returns the destination ``Path`` on success.
    """
    validate_descriptor(desc)

    sock = socket_path(host_alias)
    if not sock.exists():
        raise TransportError(
            f"No ControlMaster socket for {host_alias} at {sock}. "
            f"Run 'codeagent ssh warm {host_alias}' first."
        )

    remote_path = f"{host_alias}:{remote_root.rstrip('/')}/{desc.relative_path}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    scp = shutil.which(scp_bin)
    if not scp:
        raise TransportError(f"{scp_bin} binary not found")

    cmd = [
        scp,
        "-o", f"ControlPath={sock}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        remote_path,
        str(dest),
    ]

    log.debug("artifact pull: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=DEFAULT_PULL_TIMEOUT)

    if proc.returncode != 0:
        raise TransportError(
            f"scp failed for {desc.artifact_id}: "
            f"{proc.stderr.strip() or 'exit code ' + str(proc.returncode)}"
        )

    log.info("artifact pulled: %s → %s", desc.artifact_id, dest)

    # Verify integrity after pull
    verify_artifact(dest, desc.sha256, desc.size)
    return dest


# ── verify ────────────────────────────────────────────────────────────


def verify_artifact(path: Path, expected_sha256: str, expected_size: int) -> bool:
    """Verify a local file matches *expected_sha256* and *expected_size*.

    Returns ``True`` on success, raises ``ValueError`` on mismatch.
    """
    if not path.is_file():
        raise ValueError(f"not a file: {path}")

    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise ValueError(
            f"size mismatch for {path.name}: "
            f"expected {expected_size}, got {actual_size}"
        )

    sha256 = hashlib.sha256()
    buf_size = 1 << 20  # 1 MiB reads
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(buf_size)
            if not chunk:
                break
            sha256.update(chunk)

    actual_hash = sha256.hexdigest()
    if actual_hash != expected_sha256:
        raise ValueError(
            f"sha256 mismatch for {path.name}: "
            f"expected {expected_sha256}, got {actual_hash}"
        )

    return True
