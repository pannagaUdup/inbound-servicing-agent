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
    PAYMENT_HISTORY = "payment_history"     # recent payments on the verified loan
    LOAN_SUMMARY = "loan_summary"           # principal, rate, EMI, EMIs left, overdue
    CHARGES_QUERY = "charges_query"         # what bounce/penal charges are (explain; disputes route instead)
    ACCOUNT_QUESTION = "account_question"   # any other question about the verified loan, answered from its facts
    RESTRICTED_INFO = "restricted_info"     # collector name, internal notes, credit score: politely declined
    OUT_OF_SCOPE = "out_of_scope"           # not about the loan at all
    ESCALATION_REQUEST = "escalation_request"  # "escalate this to the compliance team / a manager"
    PAYMENT_DIFFICULTY = "payment_difficulty"  # "can't pay in full this month": offered part-pay / waiver / settlement
    POLICY_QUESTION = "policy_question"     # "how does approval work?", answered from the policy document
    FAQ = "faq"
    DISPUTE = "dispute"
    COMPLAINT = "complaint"
    CHARGE_REVERSAL = "charge_reversal"
    WAIVER_REQUEST = "waiver_request"
    SETTLEMENT_REQUEST = "settlement_request"
    CASE_STATUS = "case_status"
    CASE_WITHDRAW = "case_withdraw"
    SESSION_INFO = "session_info"         # "which loan am I logged in to?"
    DISTRESS = "distress"
    LEGAL_THREAT = "legal_threat"
    HARASSMENT_COMPLAINT = "harassment_complaint"
    GREETING = "greeting"
    UNKNOWN = "unknown"


# Intents whose handler touches account data: verification is mandatory first.
ACCOUNT_INTENTS: frozenset[Intent] = frozenset({
    Intent.PAYMENT_CLAIM, Intent.RECEIPT_REQUEST, Intent.STATEMENT_REQUEST,
    Intent.FORECLOSURE_QUOTE, Intent.EMI_DUE_QUERY, Intent.PAY_NOW,
    Intent.PAYMENT_HISTORY, Intent.LOAN_SUMMARY, Intent.CHARGES_QUERY, Intent.ACCOUNT_QUESTION, Intent.PAYMENT_DIFFICULTY, Intent.ESCALATION_REQUEST,
    Intent.WAIVER_REQUEST, Intent.SETTLEMENT_REQUEST, Intent.CASE_STATUS, Intent.CASE_WITHDRAW,
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
    source_message_id: str | None = None    # the message this item came from (links claims to media files)
    error: str | None = None                # set when perception failed; the agent asks the borrower instead


class InboundMessage(BaseModel):
    message_id: str
    sender: str                            # WhatsApp number, any format; normalised by T1
    type: Literal["text", "image", "voice", "button"] = "text"
    text: str | None = None
    media: MediaEvidence | None = None
    # Raw WhatsApp media reference (image/voice): the worker downloads bytes by this id,
    # then perception produces `media`. Additive, nullable.
    media_id: str | None = None
    media_mime: str | None = None
    received_at: datetime = Field(default_factory=datetime.utcnow)
    # message_id -> Meta media_id for every media message in a merged batch (audit link).
    media_handles: dict[str, str] = Field(default_factory=dict)

    def media_handle_for(self, message_id: str | None) -> str | None:
        if message_id and message_id in self.media_handles:
            return self.media_handles[message_id]
        return self.media_id if message_id in (None, self.message_id) else None
    # Batching (Nov 2026, additive): a turn built from several messages sent close together.
    # `media` stays for single-message producers; batched turns put every item here, in order.
    media_items: list[MediaEvidence] = Field(default_factory=list)
    batched_message_ids: list[str] = Field(default_factory=list)
    # Every piece of words in the turn, in the order the borrower sent them:
    # [{"message_id", "type": "text"|"voice"|"image", "text"}]. Typed text, voice transcripts and
    # text read from images are interleaved as sent, so "voice note, then typed UTR" reads that way.
    segments: list[dict[str, Any]] = Field(default_factory=list)

    def ordered_text_parts(self) -> list[str]:
        if self.segments:
            return [s["text"] for s in self.segments if s.get("text")]
        parts = [self.text or ""]
        for ev in self.all_media():
            parts += [ev.transcript or "", ev.ocr_text or ""]
        return [p for p in parts if p]

    def all_media(self) -> list[MediaEvidence]:
        """Every media item in this turn, in the order the borrower sent them."""
        if self.media_items:
            return list(self.media_items)
        return [self.media] if self.media else []

    @classmethod
    def merge_batch(cls, messages: list["InboundMessage"]) -> "InboundMessage":
        """Fold messages from one borrower (already in send order) into a single turn.

        Text is joined in order; every media item is kept; the first message's id is the
        turn id and all ids are kept for audit and dedupe. The batching worker (T2) calls this.
        """
        if not messages:
            raise ValueError("cannot merge an empty batch")
        if len({m.sender for m in messages}) != 1:
            raise ValueError("a batch must come from one sender")
        if len(messages) == 1:
            m = messages[0]
            if m.media and not m.media.source_message_id:
                m = m.model_copy(update={"media": m.media.model_copy(update={"source_message_id": m.message_id})})
            return m
        texts = [m.text.strip() for m in messages if m.text and m.text.strip()]
        # Every media item remembers which message it came from (links claims to the file).
        media = [ev if ev.source_message_id else ev.model_copy(update={"source_message_id": m.message_id})
                 for m in messages for ev in m.all_media()]
        types = {m.type for m in messages}
        kind = "image" if "image" in types else "voice" if "voice" in types else "text"
        segments = []
        for m in messages:
            if m.text and m.text.strip():
                segments.append({"message_id": m.message_id, "type": "text", "text": m.text.strip()})
            for ev in m.all_media():
                if ev.transcript:
                    segments.append({"message_id": m.message_id, "type": "voice", "text": ev.transcript})
                if ev.ocr_text:
                    segments.append({"message_id": m.message_id, "type": "image", "text": ev.ocr_text})
        first = messages[0]
        return cls(message_id=first.message_id, sender=first.sender, type=kind,
                   text="\n".join(texts) or None, media=None, media_items=media,
                   batched_message_ids=[m.message_id for m in messages], segments=segments,
                   media_handles={m.message_id: m.media_id for m in messages if m.media_id},
                   received_at=first.received_at)


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
    # True when this message id was already handled: the result is the original one, and the
    # caller must NOT send the replies again.
    duplicate: bool = False
    # What the agent understood this turn (same payload as the turn_normalized audit event).
    normalized: dict[str, Any] | None = None
    # Work the worker must schedule after replying, e.g. {"kind": "recheck_claim", "delay_seconds": 120, ...}.
    followups: list[dict[str, Any]] = Field(default_factory=list)
