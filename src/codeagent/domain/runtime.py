from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class RunContext:
    review_key: str
    generation: int = 1
    run_id: str = ""
    request_id: str = ""
    task_msg_id: str = ""
    swarm_session_id: str = ""
    manager_id: str = "manager"
    mailbox_agent_id: str = "oracle"
    mailbox_root: str = ""
    backend_session_id: str = ""
    host: str = "__local__"
    workdir: str = ""
    hard_deadline: float = 0.0
