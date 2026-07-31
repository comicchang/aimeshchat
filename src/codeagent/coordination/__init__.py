"""Coordination subsystem — cross-host mailbox dispatch.

Mailbox protocol (CLI, models, store) lives in codeagent.mailbox.
Cross-host dispatch uses codeagent.transport.ssh wire protocol.
"""
from __future__ import annotations
