"""OMP adapter — integration with oh-my-pi agent framework.

Provides identity registration, notification hooks, and mailbox
plugin integration for OMP-managed agent processes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def register_identity(session_id: str, worker_id: str, identity_file: Optional[str] = None) -> Path:
    """Register agent identity for OMP mailbox plugin.

    Writes identity JSON to the file specified by OMP_MAILBOX_IDENTITY_FILE
    env var, or the provided identity_file path.

    The OMP plugin polls this file and activates when valid JSON appears.
    """
    if identity_file is None:
        identity_file = os.environ.get("OMP_MAILBOX_IDENTITY_FILE", "")
    if not identity_file:
        # Generate default path
        identity_dir = Path.home() / ".omp" / "mailbox-identity"
        identity_dir.mkdir(parents=True, exist_ok=True)
        import random
        import time
        token = f"{int(time.time())}_{random.randint(1000, 9999)}"
        identity_file = str(identity_dir / f"{token}.json")

    path = Path(identity_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "session_id": session_id,
        "worker_id": worker_id,
    }))
    return path


def unregister_identity(identity_file: str) -> None:
    """Remove identity file on session shutdown."""
    try:
        Path(identity_file).unlink(missing_ok=True)
    except OSError:
        pass
