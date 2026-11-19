"""Audit / event model (owner: T1, persisted by T2, consumed by T3 evaluators)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EventType(str, Enum):
    INBOUND = "inbound"
    TURN_NORMALIZED = "turn_normalized"   # what the agent understood: merged text, media, tasks
    INJECTION_DETECTED = "injection_detected"
    TRANSLATED = "translated"
    TRANSLATION_FAILED = "translation_failed"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_OTP_SENT = "verification_otp_sent"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"
    SECURITY_ALERT = "security_alert"
    TOOL_CALL = "tool_call"
    TOOL_DENIED = "tool_denied"
    TICKET_CREATED = "ticket_created"
    CASE_CREATED = "case_created"
    CASE_SUPERSEDED = "case_superseded"
    PAYMENT_VERDICT = "payment_verdict"
    OUTPUT_BLOCKED = "output_blocked"
    HANDOFF = "handoff"
    TRIAGE = "triage"                     # compliance category, sentiment, hardship, priority
    POLICY_APPLIED = "policy_applied"     # policy clause IDs the agent applied this turn
    OUTBOUND = "outbound"


class Event(BaseModel):
    type: EventType
    conversation_id: str
    at: datetime = Field(default_factory=datetime.utcnow)
    data: dict[str, Any] = Field(default_factory=dict)
