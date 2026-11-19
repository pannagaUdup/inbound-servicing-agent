"""Conversation state, intents and the verified-account scope (owner: T1)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Intent(str, Enum):
    PAYMENT_CLAIM = "payment_claim"
    RECEIPT_REQUEST = "receipt_request"
    STATEMENT_REQUEST = "statement_request"
    FORECLOSURE_QUOTE = "foreclosure_quote"
    EMI_DUE_QUERY = "emi_due_query"
    PAY_NOW = "pay_now"
    FAQ = "faq"
    DISPUTE = "dispute"
    COMPLAINT = "complaint"
    CHARGE_REVERSAL = "charge_reversal"
    WAIVER_REQUEST = "waiver_request"
    SETTLEMENT_REQUEST = "settlement_request"
    CASE_STATUS = "case_status"
    DISTRESS = "distress"
    LEGAL_THREAT = "legal_threat"
    HARASSMENT_COMPLAINT = "harassment_complaint"
    GREETING = "greeting"
    UNKNOWN = "unknown"


# Intents whose handler touches account data: verification is mandatory first.
ACCOUNT_INTENTS: frozenset[Intent] = frozenset({
    Intent.PAYMENT_CLAIM, Intent.RECEIPT_REQUEST, Intent.STATEMENT_REQUEST,
    Intent.FORECLOSURE_QUOTE, Intent.EMI_DUE_QUERY, Intent.PAY_NOW,
    Intent.WAIVER_REQUEST, Intent.SETTLEMENT_REQUEST, Intent.CASE_STATUS,
})

# Intents that are routed to a human queue, never resolved by the agent.
ROUTED_INTENTS: frozenset[Intent] = frozenset({
    Intent.DISPUTE, Intent.COMPLAINT, Intent.CHARGE_REVERSAL,
    Intent.WAIVER_REQUEST, Intent.SETTLEMENT_REQUEST,
    Intent.DISTRESS, Intent.LEGAL_THREAT, Intent.HARASSMENT_COMPLAINT,
})

HANDOFF_INTENTS: frozenset[Intent] = frozenset({
    Intent.DISTRESS, Intent.LEGAL_THREAT, Intent.HARASSMENT_COMPLAINT,
})


class MediaEvidence(BaseModel):
    """Typed output of the perception pipeline (T3). The agent never sees raw media."""

    kind: Literal["upi_receipt", "bank_statement", "id_document", "voice_note", "other"]
    transcript: str | None = None          # voice notes: English text the planner reads
    utr: str | None = None                 # receipts: only set when confident (see field_confidence)
    amount: float | None = None
    txn_date: str | None = None            # ISO date
    payee_vpa: str | None = None           # may be masked by the payment app ("....ect@cnbank")
    status: str | None = None              # success | pending | failed
    ocr_text: str | None = None            # screened for injection like any user text
    confidence: float = 1.0
    # Optional additions (Nov 2026). All default to None/empty so older producers still work.
    original_transcript: str | None = None  # voice notes: borrower's own words, for audit and number checks
    language: str | None = None             # "kn", "hi", "en", ...
    txn_time: str | None = None             # "21:45"
    payee_name: str | None = None
    field_confidence: dict[str, float] = Field(default_factory=dict)
    low_confidence_fields: list[str] = Field(default_factory=list)  # extracted but below threshold, not used
    provider: str | None = None             # "sarvam-docai", "sarvam-stt", "stub"
    error: str | None = None                # set when perception failed; the agent asks the borrower instead


class InboundMessage(BaseModel):
    message_id: str
    sender: str                            # WhatsApp number, any format; normalised by T1
    type: Literal["text", "image", "voice", "button"] = "text"
    text: str | None = None
    media: MediaEvidence | None = None
    received_at: datetime = Field(default_factory=datetime.utcnow)


class Task(BaseModel):
    intent: Intent
    confidence: float = 1.0
    slots: dict[str, Any] = Field(default_factory=dict)


class AuthScope(BaseModel):
    """Set only by the verification subflow. The gateway injects account_id into every tool call."""

    account_id: str
    customer_id: str
    contact_id: str
    method: Literal["account+mobile+otp", "account+mobile"]
    verified_at: datetime
    expires_at: datetime


class VerificationState(BaseModel):
    stage: Literal["none", "awaiting_account", "awaiting_otp", "verified", "locked"] = "none"
    # Internal only: the account the OTP was sent for (None when details did not match).
    candidate_account_id: str | None = None
    candidate_contact_id: str | None = None
    candidate_customer_id: str | None = None
    candidate_contact_added_on: str | None = None
    otp_request_id: str | None = None
    otp_attempts: int = 0
    failed_attempts: int = 0
    locked_until: datetime | None = None


class PendingChange(BaseModel):
    """A material change to an open waiver/settlement case, awaiting borrower confirmation."""

    case_id: str
    from_version: int
    request: dict[str, Any]


class Reply(BaseModel):
    text: str
    template_id: str
    buttons: list[str] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)


class TurnResult(BaseModel):
    conversation_id: str
    replies: list[Reply]
    verified: bool
    intents: list[Intent]
    tickets: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    resolution: Literal["auto", "routed", "pending_verification", "clarify", "open"] = "open"
