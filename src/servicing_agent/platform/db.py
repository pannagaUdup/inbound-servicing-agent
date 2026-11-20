"""Persistence (owner: T2). SQLite by default, Postgres via SA_DATABASE_URL.

Engine-agnostic SQLAlchemy 2.0 models for the whole servicing data model
(see docs/DATA-MODEL.md). Relationships are modelled as indexed columns with a
``# -> table`` comment rather than hard ForeignKey constraints — matching the
existing style and keeping the read-only D1/D2/D7 mirror safe to leave empty in dev.

Money uses Numeric(14,2) in the tables added here. The original case tables use
Float; unify on Numeric when convenient (docs/DATA-MODEL.md, open question 1).
Timestamps are naive UTC (datetime.utcnow) to match the original tables; move to
timezone-aware UTC in one pass later (open question 6).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, Integer, Numeric, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


# =====================================================================================
# Waiver / settlement cases  (owner: T2) — UNCHANGED: platform/cases.py depends on these
# =====================================================================================

class Case(Base):
    __tablename__ = "cases"
    case_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(16), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    restarts: Mapped[int] = mapped_column(Integer, default=0)
    gaming_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    assignee: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # ops user the case lands on
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CaseVersion(Base):
    __tablename__ = "case_versions"
    __table_args__ = (UniqueConstraint("case_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    type: Mapped[str] = mapped_column(String(16))
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    request: Mapped[dict] = mapped_column(JSON)
    diff: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supersede_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    offer_outstanding: Mapped[bool] = mapped_column(Boolean, default=False)
    etag: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(16))
    rationale: Mapped[str] = mapped_column(Text)
    conditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TraceSpan(Base):
    """Per-node execution record for one agent turn (SA_TRACING). Powers the Traces console.

    Separate from ``audit_events`` on purpose: spans are diagnostic, high-volume and never part of
    TurnResult.events. Inputs / outputs are masked summaries (same masking as the audit log).
    """

    __tablename__ = "trace_spans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)          # 0 = the turn itself, 1.. = nodes in run order
    name: Mapped[str] = mapped_column(String(32))                 # "turn" or a graph node name
    status: Mapped[str] = mapped_column(String(16))               # succeeded | failed
    started_at: Mapped[datetime] = mapped_column(DateTime)        # India wall clock, like audit_events
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    input: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditEvent(Base):
    """Append-only. Every inbound message, decision, tool call and outbound message."""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    # India time (IST, Asia/Kolkata) wall clock, no offset stored. Rows before 2026-11-20 04:10 IST were
    # converted from UTC. Internal clocks (sessions, OTPs, claim windows) stay in UTC.
    at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None))
    # Added for cross-entity tracing (docs/DATA-MODEL.md §8); nullable so old rows are fine.
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class MaterialChangeConfirmation(Base):
    """Borrower confirmation that precedes a supersede (owner: T2)."""

    __tablename__ = "material_change_confirmations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(32), index=True)          # -> cases
    version_id: Mapped[int] = mapped_column(Integer, index=True)          # -> case_versions.id
    diff: Mapped[dict] = mapped_column(JSON)
    borrower_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ContextPack(Base):
    """Source-linked reviewer context, one per case version (owner: T2)."""

    __tablename__ = "context_packs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(Integer, index=True)          # -> case_versions.id
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# =====================================================================================
# Conversation, session & media  (owner: T1)
# =====================================================================================

class ConversationState(Base):
    """Write-through snapshot of the live in-memory conversation dict (session scope, verification,
    pending actions, transcript, events, tickets) so a restart doesn't drop verified borrowers or
    pending YES/NO. Internal store; holds borrower transcript text (same data already in memory)."""
    __tablename__ = "conversation_state"
    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sender: Mapped[str] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(32), default="new")
    redis_partition_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Session(Base):
    """Verified scope. One account per session; re-verifying replaces account_id."""

    __tablename__ = "sessions"
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)   # -> conversations
    sender_mobile_hash: Mapped[str] = mapped_column(String(64), index=True)
    customer_id: Mapped[str | None] = mapped_column(String(32), nullable=True)   # -> customers
    account_id: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)  # -> accounts; NULL until verified
    mobile_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    method: Mapped[str | None] = mapped_column(String(32), nullable=True)  # account+mobile+otp | account+mobile
    sim_swap_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)  # idle 30 min
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)           # absolute 120 min


class Media(Base):
    """Raw bytes live in S3/MinIO (KMS); the DB keeps only the key."""

    __tablename__ = "media"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    s3_key: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(32))  # upi_receipt | bank_statement | id_document | voice_note | other
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)   # -> conversations
    direction: Mapped[str] = mapped_column(String(8))                      # inbound | outbound
    channel_message_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)  # wamid, dedupe
    type: Mapped[str] = mapped_column(String(16))                          # text | voice | image | button
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # -> media
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 3-second batching
    template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # outbound template
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Extraction(Base):
    """Perception output — the typed MediaEvidence the agent sees (owner: T3)."""

    __tablename__ = "extractions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_id: Mapped[str] = mapped_column(String(64), index=True)          # -> media
    kind: Mapped[str] = mapped_column(String(16))                          # ASR | VLM | OCR
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    fields: Mapped[dict] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# =====================================================================================
# Verification & security  (owner: T1)
# =====================================================================================

class VerificationAttempt(Base):
    __tablename__ = "verification_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)   # -> conversations
    sender_mobile_hash: Mapped[str] = mapped_column(String(64), index=True)  # 5/sender/24h rule
    account_number_submitted: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)  # 3-numbers/account rule
    outcome: Mapped[str] = mapped_column(String(24))  # format_invalid|no_match|uniform_reply|otp_sent|verified
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class OtpChallenge(Base):
    __tablename__ = "otp_challenges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)        # -> sessions
    contact_id: Mapped[str] = mapped_column(String(32))                    # -> contacts (number on record)
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)              # max 3
    status: Mapped[str] = mapped_column(String(16), default="pending")     # pending|verified|failed|expired
    expires_at: Mapped[datetime] = mapped_column(DateTime)                 # 5-min validity
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Lockout(Base):
    __tablename__ = "lockouts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(64), index=True)           # sender mobile_hash OR account_id
    reason: Mapped[str] = mapped_column(String(32))                        # otp_3_fail|sender_24h|account_cross_number
    locked_until: Mapped[datetime] = mapped_column(DateTime)               # 30-min lock
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SecurityEvent(Base):
    __tablename__ = "security_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(24))  # block|fraud|sim_swap|account_guessing
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# =====================================================================================
# Payments, claims & receipts  (owner: T2 / T3)
# =====================================================================================

class PaymentClaim(Base):
    """A claim is a search, never proof. Only an exact D7 match is PAID."""

    __tablename__ = "payment_claims"
    # One UTR can be claimed once per account (replay guard).
    __table_args__ = (UniqueConstraint("account_id", "utr", name="uq_claim_account_utr"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)   # -> conversations
    account_id: Mapped[str] = mapped_column(String(16), index=True)        # -> accounts
    source: Mapped[str] = mapped_column(String(16))                        # screenshot | voice | text
    media_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # -> media
    utr: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payee_vpa: Mapped[str | None] = mapped_column(String(64), nullable=True)
    matched_txn_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # -> payment_transactions
    result: Mapped[str | None] = mapped_column(String(16), nullable=True)  # PAID|EDITED|REUSED|NOT_FOUND
    field_diff: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    queue: Mapped[str | None] = mapped_column(String(32), nullable=True)   # fraud_review | payment_reconciliation
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Idempotency + audit (Nov 2026): one row per (account, UTR); resends update it instead of adding rows.
    reasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    ticket_id: Mapped[str | None] = mapped_column(String(32), nullable=True)   # reused on resend, never duplicated
    attempts: Mapped[int] = mapped_column(Integer, default=1)                 # how many times this UTR was sent
    first_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Receipt(Base):
    """Receipt only from a D7 txn_id — never a free-text amount. One receipt per transaction."""

    __tablename__ = "receipts"
    __table_args__ = (UniqueConstraint("txn_id", name="uq_receipt_txn"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    txn_id: Mapped[str] = mapped_column(String(32), index=True)            # -> payment_transactions (required)
    account_id: Mapped[str] = mapped_column(String(16), index=True)        # -> accounts
    document_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# =====================================================================================
# Tickets & guard events  (owner: T2 / T1)
# =====================================================================================

class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str | None] = mapped_column(String(16), nullable=True, unique=True, index=True)  # TKT000123
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)   # -> conversations
    account_id: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)  # -> accounts
    request_type: Mapped[str] = mapped_column(String(32))                  # key into config/routing_matrix.yaml
    routing_queue: Mapped[str] = mapped_column(String(48))
    priority: Mapped[str] = mapped_column(String(4))                       # P1|P2|P3
    sla_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")        # open | resolved
    assignee: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # ops user the ticket lands on
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CollectionAgency(Base):
    """Third-party collection agencies that field on-ground recovery for a set of accounts.
    Complaints (harassment/dispute/complaint) are attributed to the account's agency."""
    __tablename__ = "collection_agencies"
    agency_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(40), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(24), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GuardEvent(Base):
    """Prompt-injection detections and output-guard blocks."""

    __tablename__ = "guard_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)   # -> conversations
    category: Mapped[str] = mapped_column(String(16))                      # injection | output
    type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# =====================================================================================
# Source systems — D1 / D2 / D7 read-only mirror  (owner: T3)
# Populated by a loader off the generated CSVs; empty in dev where store.py serves CSV.
# =====================================================================================

class Customer(Base):
    __tablename__ = "customers"
    customer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    dob: Mapped[str | None] = mapped_column(String(10), nullable=True)
    preferred_language: Mapped[str | None] = mapped_column(String(4), nullable=True)
    city: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Account(Base):
    __tablename__ = "accounts"
    account_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(32), index=True)       # -> customers
    product: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sanction_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    principal_outstanding: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    emi_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    interest_rate: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    tenure_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tenure_left: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dpd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overdue_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    bounce_charges: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    penal_charges: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    foreclosure_charge_pct: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    next_due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Internal-only — never exposed by the gateway:
    collector_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    internal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    bureau_score: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Contact(Base):
    __tablename__ = "contacts"
    contact_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(32), index=True)       # -> customers
    mobile_hash: Mapped[str] = mapped_column(String(64), index=True)       # stored hashed; never a lookup key alone
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_on: Mapped[str | None] = mapped_column(String(10), nullable=True)  # < 7 days -> SIM-swap flag


class AccountContact(Base):
    __tablename__ = "account_contacts"
    __table_args__ = (UniqueConstraint("account_id", "contact_id", name="uq_account_contact"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(16), index=True)        # -> accounts
    contact_id: Mapped[str] = mapped_column(String(32), index=True)        # -> contacts
    role: Mapped[str] = mapped_column(String(16), default="borrower")      # borrower | co_borrower


class PaymentTransaction(Base):
    """D7 feed mirror. Empty account_id ~ suspense / unmapped credit."""

    __tablename__ = "payment_transactions"
    txn_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    utr: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    account_id: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)  # -> accounts
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str | None] = mapped_column(String(12), nullable=True)  # success|failed|pending|reversed
    txn_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    settled_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    payee_vpa: Mapped[str | None] = mapped_column(String(64), nullable=True)


class WaiverHistory(Base):
    """Past waivers/settlements per account (feeds the context pack's prior_concessions)."""

    __tablename__ = "waiver_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(16), index=True)        # -> accounts
    type: Mapped[str] = mapped_column(String(16))                          # waiver | settlement
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    components: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. "penal|bounce"
    decided_on: Mapped[str | None] = mapped_column(String(10), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)  # approved | rejected


# =====================================================================================
# Evaluation  (owner: T3)
# =====================================================================================

class EvalDataset(Base):
    __tablename__ = "eval_datasets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    split: Mapped[str] = mapped_column(String(16))  # dev | held_out | regression
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scores: Mapped[dict] = mapped_column(JSON)
    hard_zero_failures: Mapped[int] = mapped_column(Integer, default=0)  # the CI gate
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# =====================================================================================
# Campaigns — outbound, admin-initiated  (owner: T1 / admin panel)  [Phase 1]
# An admin selects accounts; the bot sends an outbound message to each account's
# registered contact (number resolved from our DB), then tracks each account's journey.
# =====================================================================================

class Campaign(Base):
    __tablename__ = "campaigns"
    campaign_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)   # admin id
    purpose: Mapped[str] = mapped_column(String(32), default="payment_confirmation")
    # Placeholder message now; a Meta-approved template_id + params come later.
    template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    template_lang: Mapped[str] = mapped_column(String(12), default="en_US")
    template_params: Mapped[list | None] = mapped_column(JSON, nullable=True)  # body placeholders, in order
    message_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|running|paused|completed|cancelled
    total_targets: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CampaignTarget(Base):
    """One selected account's journey within a campaign."""

    __tablename__ = "campaign_targets"
    __table_args__ = (UniqueConstraint("campaign_id", "account_id", name="uq_campaign_account"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(32), index=True)       # -> campaigns
    account_id: Mapped[str] = mapped_column(String(16), index=True)        # -> accounts
    contact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)   # -> contacts (chosen recipient)
    to_mobile_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # set when the reply opens a session
    out_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # wamid of the outbound send
    # Per-account journey state machine:
    # queued|sent|delivered|read|acknowledged|otp_sent|verified|confirmed|branched|failed|no_response|opted_out|closed
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    journey: Mapped[dict | None] = mapped_column(JSON, nullable=True)      # freeform branch data
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# Dev-grade additive migration: columns added to existing tables after they were first
# created. create_all() never ALTERs, so we add any missing column here (nullable only).
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "tickets": {"ticket_id": "VARCHAR(16)", "status": "VARCHAR(16) DEFAULT 'open'",
                "assignee": "VARCHAR(64)", "resolved_at": "TIMESTAMP"},
    "cases": {"assignee": "VARCHAR(64)"},
}


def _ensure_columns(engine) -> None:
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all already built it with every column
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def make_session_factory(url: str):
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    engine = create_engine(url, future=True, **kwargs)
    Base.metadata.create_all(engine)
    _ensure_columns(engine)
    return sessionmaker(engine, expire_on_commit=False, future=True)
