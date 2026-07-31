"""OMP plugin hooks for swarm kernel lifecycle.

These functions are called by the OMP runner plugin to wire agent
lifecycle events into the SwarmKernel session/roster system.

Usage (in OMP plugin YAML or Python hook):

    from codeagent.hooks.swarm_hooks import (
        on_agent_start,
        on_agent_message,
        on_agent_stop,
    )

    # In plugin on_agent_start:
    on_agent_start(session_id="s1", agent_id="worker-1",
                   host_alias="__local__", backend="omp")

    # In plugin on_agent_message (inbound dispatch):
    on_agent_message(session_id="s1", agent_id="worker-1",
                     msg_dict=raw_message)

    # In plugin on_agent_stop:
    on_agent_stop(session_id="s1", agent_id="worker-1")
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from codeagent.mailbox.store import MailboxStore
from codeagent.swarm.kernel import LocalDeliverySink, SwarmKernel
from codeagent.swarm.model import AgentLocation, Envelope

# Module-level kernel instance (lazy singleton for OMP process lifetime)
_kernel: Optional[SwarmKernel] = None
_store: Optional[MailboxStore] = None


def _get_kernel(store_root: Optional[Path] = None) -> tuple[SwarmKernel, MailboxStore]:
    """Get or create the module-level kernel singleton."""
    global _kernel, _store
    if _kernel is None:
        _store = MailboxStore(root=store_root)
        _kernel = SwarmKernel(store=_store, sink=LocalDeliverySink(_store))
    return _kernel, _store


def reset() -> None:
    """Reset the module-level kernel singleton (for testing)."""
    global _kernel, _store
    _kernel = None
    _store = None


def on_agent_start(
    session_id: str,
    agent_id: str,
    host_alias: str = "__local__",
    backend: str = "omp",
    store_root: Optional[Path] = None,
) -> dict:
    """Register an agent when it starts (OMP on_agent_start hook).

    Parameters
    ----------
    session_id : str
        The swarm session this agent belongs to.
    agent_id : str
        The agent's unique identifier within the session.
    host_alias : str
        SSH alias or "__local__" for co-located agents.
    backend : str
        Runner backend: "cli", "omp", or "tmux".
    store_root : Path, optional
        Override MAILBOX_ROOT for the store.

    Returns
    -------
    dict
        Registration info with agent_id, session_id, host_alias, backend.
    """
    kernel, _ = _get_kernel(store_root)
    loc = AgentLocation(agent_id=agent_id, host_alias=host_alias, backend=backend)
    reg = kernel.register(loc, session_id)
    return {
        "agent_id": reg.agent_id,
        "session_id": reg.session_id,
        "host_alias": reg.location.host_alias,
        "backend": reg.location.backend,
    }


def on_agent_message(
    session_id: str,
    agent_id: str,
    msg_dict: dict[str, Any],
    store_root: Optional[Path] = None,
) -> dict:
    """Dispatch an inbound message to the swarm kernel (OMP on_agent_message hook).

    Validates the message and routes it according to the swarm protocol.
    Used for inbound dispatch — the message arrives from an external source
    and needs to be routed through the kernel.

    Parameters
    ----------
    session_id : str
        The swarm session.
    agent_id : str
        The sender agent ID.
    msg_dict : dict
        Raw message dict with at least 'to', 'kind', 'subject', 'body'.
        'to' may be an agent_id (direct), '#channel_id' (channel), or
        '*' (broadcast).

    Returns
    -------
    dict
        SendReceipt or list of DeliveryReceipts as a dict.
    """
    kernel, _ = _get_kernel(store_root)

    to_id = msg_dict.get("to", "")
    kind = msg_dict.get("kind", "TASK")
    subject = msg_dict.get("subject", "")
    body = msg_dict.get("body", "")

    env = Envelope(subject=subject, body=body, kind=kind)

    if to_id == "*":
        receipts = kernel.broadcast(session_id, agent_id, env)
        return {"broadcast": True, "recipients": len(receipts)}
    elif to_id.startswith("#"):
        channel_id = to_id[1:]
        receipt = kernel.channel(session_id, agent_id, channel_id, env)
        return {"msg_id": receipt.msg_id, "status": receipt.status, "target": receipt.target}
    else:
        receipt = kernel.direct(session_id, agent_id, to_id, env)
        return {"msg_id": receipt.msg_id, "status": receipt.status, "target": receipt.target}


def on_agent_stop(
    session_id: str,
    agent_id: str,
    store_root: Optional[Path] = None,
) -> dict:
    """Unregister an agent when it stops (OMP on_agent_stop hook).

    Parameters
    ----------
    session_id : str
        The swarm session.
    agent_id : str
        The agent being unregistered.
    store_root : Path, optional
        Override MAILBOX_ROOT for the store.

    Returns
    -------
    dict
        Confirmation with agent_id and session_id.
    """
    kernel, _ = _get_kernel(store_root)
    kernel.unregister(session_id, agent_id)
    return {"unregistered": True, "agent_id": agent_id, "session_id": session_id}
