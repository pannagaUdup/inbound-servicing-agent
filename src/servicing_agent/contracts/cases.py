"""Waiver/settlement case and decision contracts (owner: T2)."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class CaseType(str, Enum):
    WAIVER = "waiver"
    SETTLEMENT = "settlement"


class CaseStatus(str, Enum):
    PENDING_APPROVER = "PENDING_APPROVER"        # L1/L2 maker (team lead / collections manager)
    PENDING_CREDIT_HEAD = "PENDING_CREDIT_HEAD"  # higher authority tier for large / principal-touching cases
    PENDING_COMPLIANCE = "PENDING_COMPLIANCE"
    APPROVED = "APPROVED"            # approved, not yet sent to the borrower
    REJECTED = "REJECTED"
    COMMUNICATED = "COMMUNICATED"    # outcome sent via compliance-approved template
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"          # the borrower cancelled it before a decision


OPEN_STATUSES = {CaseStatus.PENDING_APPROVER, CaseStatus.PENDING_CREDIT_HEAD,
                 CaseStatus.PENDING_COMPLIANCE, CaseStatus.APPROVED}


class CaseRequest(BaseModel):
    """Structured request the planner extracts. Code, not the LLM, decides if a change is material."""

    type: CaseType
    amount: float | None = None
    components: list[Literal["penal", "bounce", "interest", "principal"]] = Field(default_factory=list)
    plan: str | None = None               # e.g. "lump_sum", "3_instalments"
    reason_category: str | None = None    # e.g. "job_loss", "medical", "business_loss"
    borrower_text: str | None = None


class DecisionIn(BaseModel):
    role: Literal["approver", "credit_head", "compliance"]
    decision: Literal["approve", "reject"]
    rationale: str
    conditions: str | None = None
    reviewer_id: str
    etag: str


class CaseVersionOut(BaseModel):
    case_id: str
    version: int
    status: CaseStatus
    request: CaseRequest
    diff: dict | None = None
    superseded_by: int | None = None
    offer_outstanding: bool = False
    etag: str
