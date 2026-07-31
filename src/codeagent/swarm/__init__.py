"""Swarm — IRC-style agent communication kernel.

Modules:
    model:   Data types (AgentLocation, Address, Envelope, Session, Roster, ACL, etc.)
    kernel:  SwarmKernel — session/roster/ACL/routing, message delivery, poll/subscribe.
"""
from codeagent.swarm.kernel import SwarmKernel
from codeagent.swarm.receiver import SwarmReceiver
from codeagent.swarm.model import (
    ACL,
    Address,
    AddressKind,
    AgentLocation,
    DeliveryReceipt,
    Envelope,
    PollResult,
    Registration,
    Roster,
    SendReceipt,
    Session,
    Subscription,
)

__all__ = [
    "ACL",
    "Address",
    "AddressKind",
    "AgentLocation",
    "DeliveryReceipt",
    "Envelope",
    "PollResult",
    "Registration",
    "Roster",
    "SendReceipt",
    "Session",
    "Subscription",
    "SwarmKernel",
    "SwarmReceiver",
]
