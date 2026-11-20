"""Versioned waiver/settlement case service (owner: T2).

The database is the source of truth. Rules:
* A material change creates version v+1 and supersedes v in one transaction.
* Every decision carries the version and etag; the update is guarded by
  ``WHERE version=:v AND status=:expected AND etag=:etag``. Zero rows -> 409.
* A superseded version can never be approved or resumed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update

from servicing_agent.contracts.cases import OPEN_STATUSES, CaseRequest, CaseStatus, CaseVersionOut, DecisionIn
from servicing_agent.platform.db import Case, CaseVersion, Decision
from servicing_agent.platform.eligibility import evaluate as evaluate_eligibility


class Conflict(Exception):
    """Decision on a stale or superseded version (HTTP 409)."""


class NotFound(Exception):
    pass


def _etag() -> str:
    return uuid.uuid4().hex[:16]


def request_diff(old: dict, new: dict) -> dict:
    keys = ["type", "amount", "components", "plan", "reason_category"]
    return {k: {"from": old.get(k), "to": new.get(k)} for k in keys if old.get(k) != new.get(k)}


def is_material_change(old: dict, new: dict) -> bool:
    """Deterministic: any change in type, amount, components, plan or reason is material."""
    return bool(request_diff(old, new))


class CaseService:
    def __init__(self, session_factory, restarts_flag_threshold: int = 3, assignee: str | None = None,
                 store=None, bands_cfg: dict | None = None):
        self.Session = session_factory
        self.restarts_flag_threshold = restarts_flag_threshold
        self.assignee = assignee  # single ops user every new case lands on (one-queue model)
        self.store = store              # for account lookup (DPD) in eligibility
        self.bands_cfg = bands_cfg or {}

    def _eligibility(self, account_id: str, request: dict) -> dict:
        account = self.store.accounts.get(account_id) if self.store else None
        return evaluate_eligibility(request, account, self.bands_cfg)

    def eligibility_for(self, case_id: str) -> dict:
        with self.Session() as s:
            case = s.get(Case, case_id)
            if not case:
                raise NotFound(case_id)
            v = s.execute(select(CaseVersion).where(CaseVersion.case_id == case_id,
                                                    CaseVersion.version == case.current_version)).scalar_one()
            return self._eligibility(case.account_id, v.request)

    # ---- reads
    def open_case_for_account(self, account_id: str) -> CaseVersionOut | None:
        with self.Session() as s:
            rows = s.execute(
                select(CaseVersion).join(Case, Case.case_id == CaseVersion.case_id)
                .where(Case.account_id == account_id, CaseVersion.version == Case.current_version,
                       CaseVersion.status.in_([st.value for st in OPEN_STATUSES]))
            ).scalars().all()
            return self._out(rows[0]) if rows else None

    def get(self, case_id: str) -> dict:
        with self.Session() as s:
            case = s.get(Case, case_id)
            if not case:
                raise NotFound(case_id)
            versions = s.execute(select(CaseVersion).where(CaseVersion.case_id == case_id)
                                 .order_by(CaseVersion.version)).scalars().all()
            decisions = s.execute(select(Decision).where(Decision.case_id == case_id)
                                  .order_by(Decision.id)).scalars().all()
            return {
                "case_id": case.case_id, "account_id": case.account_id, "conversation_id": case.conversation_id,
                "current_version": case.current_version,
                "restarts": case.restarts, "gaming_flag": case.gaming_flag, "assignee": case.assignee,
                "eligibility": self._eligibility(
                    case.account_id,
                    next((v.request for v in versions if v.version == case.current_version), {})),
                "versions": [self._out(v).model_dump(mode="json") for v in versions],
                "decisions": [{"version": d.version, "role": d.role, "decision": d.decision,
                               "rationale": d.rationale, "conditions": d.conditions, "reviewer_id": d.reviewer_id,
                               "at": d.created_at.isoformat()} for d in decisions],
            }

    def get_version(self, case_id: str, version: int) -> CaseVersionOut:
        with self.Session() as s:
            v = s.execute(select(CaseVersion).where(CaseVersion.case_id == case_id,
                                                    CaseVersion.version == version)).scalar_one_or_none()
            if not v:
                raise NotFound(f"{case_id} v{version}")
            return self._out(v)

    # ---- writes
    def create(self, account_id: str, conversation_id: str, req: CaseRequest) -> CaseVersionOut:
        case_id = f"CASE{uuid.uuid4().hex[:10].upper()}"
        with self.Session.begin() as s:
            s.add(Case(case_id=case_id, account_id=account_id, conversation_id=conversation_id,
                       current_version=1, assignee=self.assignee))
            v = CaseVersion(case_id=case_id, version=1, status=CaseStatus.PENDING_APPROVER.value, type=req.type.value,
                            amount=req.amount, request=req.model_dump(mode="json"), etag=_etag())
            s.add(v)
            s.flush()
            return self._out(v)

    def supersede(self, case_id: str, from_version: int, req: CaseRequest, reason: str,
                  borrower_message_id: str | None = None) -> CaseVersionOut:
        with self.Session.begin() as s:
            case = s.get(Case, case_id, with_for_update=True)
            if not case:
                raise NotFound(case_id)
            if case.current_version != from_version:
                raise Conflict(f"case {case_id} is at v{case.current_version}, not v{from_version}")
            old = s.execute(select(CaseVersion).where(CaseVersion.case_id == case_id,
                                                      CaseVersion.version == from_version)).scalar_one()
            if old.status == CaseStatus.SUPERSEDED.value:
                raise Conflict("already superseded")
            offer_outstanding = old.status == CaseStatus.COMMUNICATED.value
            new_version = from_version + 1
            res = s.execute(
                update(CaseVersion)
                .where(CaseVersion.id == old.id, CaseVersion.status == old.status, CaseVersion.etag == old.etag)
                .values(status=CaseStatus.SUPERSEDED.value, superseded_by=new_version,
                        supersede_reason=f"{reason} (msg {borrower_message_id})" if borrower_message_id else reason,
                        offer_outstanding=offer_outstanding, etag=_etag())
            )
            if res.rowcount != 1:
                raise Conflict("concurrent decision on the old version")
            new_req = req.model_dump(mode="json")
            v = CaseVersion(case_id=case_id, version=new_version, status=CaseStatus.PENDING_APPROVER.value,
                            type=req.type.value, amount=req.amount, request=new_req,
                            diff=request_diff(old.request, new_req), etag=_etag())
            s.add(v)
            case.current_version = new_version
            recent = case.created_at >= datetime.utcnow() - timedelta(days=7)
            case.restarts += 1
            if recent and case.restarts > self.restarts_flag_threshold:
                case.gaming_flag = True
            s.flush()
            # TODO(T2): cancel the old version's approval workflow and notify its reviewers.
            return self._out(v)

    # Which status a role is allowed to act on.
    _ROLE_EXPECTED = {
        "approver": CaseStatus.PENDING_APPROVER,
        "credit_head": CaseStatus.PENDING_CREDIT_HEAD,
        "compliance": CaseStatus.PENDING_COMPLIANCE,
    }

    def decide(self, case_id: str, version: int, d: DecisionIn) -> CaseVersionOut:
        expected = self._ROLE_EXPECTED[d.role]
        with self.Session.begin() as s:
            case = s.get(Case, case_id)
            ver = s.execute(select(CaseVersion).where(CaseVersion.case_id == case_id,
                                                      CaseVersion.version == version)).scalar_one_or_none()
            if not case or not ver:
                raise NotFound(f"{case_id} v{version}")
            if d.decision == "reject":
                new_status = CaseStatus.REJECTED
            elif d.role == "approver":
                # Route to the credit-head tier for large / principal-touching cases; else straight to compliance.
                elig = self._eligibility(case.account_id, ver.request)
                new_status = CaseStatus.PENDING_CREDIT_HEAD if elig["requires_credit_head"] else CaseStatus.PENDING_COMPLIANCE
            elif d.role == "credit_head":
                new_status = CaseStatus.PENDING_COMPLIANCE
            else:  # compliance
                new_status = CaseStatus.APPROVED
            res = s.execute(
                update(CaseVersion)
                .where(CaseVersion.case_id == case_id, CaseVersion.version == version,
                       CaseVersion.status == expected.value, CaseVersion.etag == d.etag)
                .values(status=new_status.value, etag=_etag())
            )
            if res.rowcount != 1:
                raise Conflict("superseded, already decided, or stale etag")
            s.add(Decision(case_id=case_id, version=version, role=d.role, decision=d.decision,
                           rationale=d.rationale, conditions=d.conditions, reviewer_id=d.reviewer_id))
        return self.get_version(case_id, version)

    @staticmethod
    def _out(v: CaseVersion) -> CaseVersionOut:
        return CaseVersionOut(case_id=v.case_id, version=v.version, status=CaseStatus(v.status),
                              request=CaseRequest(**v.request), diff=v.diff, superseded_by=v.superseded_by,
                              offer_outstanding=v.offer_outstanding, etag=v.etag)
