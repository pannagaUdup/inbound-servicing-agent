"""Traces console API (read-only): per-turn node traces and the approval / compliance tracker.

Everything here reads ``trace_spans`` (written when SA_TRACING=true), ``audit_events``, and the
case tables. No endpoint writes, so it cannot affect the conversation or the approval flow.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select

from servicing_agent.platform.db import AuditEvent, Case, CaseVersion, Decision, TraceSpan

# Order in which a case moves through the maker-checker tiers.
STAGES = ("approver", "credit_head", "compliance")
PENDING = {"approver": "PENDING_APPROVER", "credit_head": "PENDING_CREDIT_HEAD", "compliance": "PENDING_COMPLIANCE"}


def _span(r: TraceSpan) -> dict:
    return {"trace_id": r.trace_id, "conversation_id": r.conversation_id, "seq": r.seq, "name": r.name,
            "status": r.status, "started_at": r.started_at.isoformat(), "duration_ms": r.duration_ms,
            "input": r.input, "output": r.output, "tool_calls": r.tool_calls or [], "error": r.error}


def _case_ids(spans: list[TraceSpan]) -> list[str]:
    seen: list[str] = []
    for r in spans:
        for c in r.tool_calls or []:
            cid = c.get("case_id")
            if cid and cid not in seen:
                seen.append(cid)
    return seen


def build_case_timeline(case: Case, versions: list[CaseVersion], decisions: list[Decision],
                        requires_credit_head: bool, now: datetime) -> dict:
    out_versions = []
    for v in versions:
        ds = [d for d in decisions if d.version == v.version]
        by_role = {d.role: d for d in ds}
        started = v.created_at
        stages, waiting_since = [], started
        rejected = any(d.decision == "reject" for d in ds)
        for role in STAGES:
            d = by_role.get(role)
            if role == "credit_head" and not d and v.status != PENDING[role] and not requires_credit_head:
                stages.append({"role": role, "state": "skipped"})
                continue
            if d:
                stages.append({"role": role, "state": "rejected" if d.decision == "reject" else "approved",
                               "reviewer": d.reviewer_id, "rationale": d.rationale, "conditions": d.conditions,
                               "decided_at": d.created_at.isoformat(),
                               "waited_seconds": int((d.created_at - waiting_since).total_seconds())})
                waiting_since = d.created_at
            elif v.status == PENDING[role]:
                stages.append({"role": role, "state": "waiting", "since": waiting_since.isoformat(),
                               "waiting_seconds": int((now - waiting_since).total_seconds())})
            else:
                stages.append({"role": role, "state": "superseded" if v.status == "SUPERSEDED" and not rejected
                               else "not_reached"})
        out_versions.append({
            "version": v.version, "status": v.status, "type": v.type, "amount": v.amount,
            "created_at": started.isoformat(), "diff": v.diff, "superseded_by": v.superseded_by,
            "supersede_reason": v.supersede_reason, "current": v.version == case.current_version,
            "stages": stages,
        })
    return {"case_id": case.case_id, "conversation_id": case.conversation_id, "current_version": case.current_version,
            "restarts": case.restarts, "gaming_flag": case.gaming_flag, "versions": out_versions}


def register_traces(r, runtime) -> None:
    @r.get("/api/traces")
    def list_traces(limit: int = 50, conversation_id: str | None = None, status: str | None = None):
        """Most recent turns first. One row per turn (the root span)."""
        q = select(TraceSpan).where(TraceSpan.seq == 0).order_by(TraceSpan.id.desc()).limit(max(1, min(limit, 200)))
        if conversation_id:
            q = q.where(TraceSpan.conversation_id == conversation_id)
        if status:
            q = q.where(TraceSpan.status == status)
        with runtime.session_factory() as s:
            rows = s.execute(q).scalars().all()
        return [{"trace_id": t.trace_id, "conversation_id": t.conversation_id, "status": t.status,
                 "started_at": t.started_at.isoformat(), "duration_ms": t.duration_ms,
                 "text": (t.input or {}).get("text"), "message_type": (t.input or {}).get("type"),
                 "resolution": (t.output or {}).get("resolution"), "intents": (t.output or {}).get("intents", []),
                 "path": (t.output or {}).get("path", []), "error": t.error} for t in rows]

    @r.get("/api/traces/case/{case_id}")
    def case_timeline(case_id: str):
        """Approval / compliance tracker: every version, each tier's state, who decided and how long it waited."""
        with runtime.session_factory() as s:
            case = s.get(Case, case_id)
            if not case:
                raise HTTPException(404, detail="case not found")
            versions = s.execute(select(CaseVersion).where(CaseVersion.case_id == case_id)
                                 .order_by(CaseVersion.version)).scalars().all()
            decisions = s.execute(select(Decision).where(Decision.case_id == case_id)
                                  .order_by(Decision.id)).scalars().all()
            convo_spans = s.execute(select(TraceSpan).where(TraceSpan.conversation_id == case.conversation_id)
                                    .order_by(TraceSpan.id)).scalars().all()
        try:
            req_ch = bool(runtime.cases.eligibility_for(case_id).get("requires_credit_head"))
        except Exception:  # noqa: BLE001 - eligibility is advisory for display
            req_ch = False
        tl = build_case_timeline(case, versions, decisions, req_ch, datetime.utcnow())
        tl["traces"] = sorted({sp.trace_id for sp in convo_spans if case_id in _case_ids([sp])})
        return tl

    @r.get("/api/traces/{trace_id}")
    def get_trace(trace_id: str):
        """Every node of one turn, in run order, with masked input, output, timing and gateway tool calls."""
        with runtime.session_factory() as s:
            spans = s.execute(select(TraceSpan).where(TraceSpan.trace_id == trace_id)
                              .order_by(TraceSpan.seq)).scalars().all()
            if not spans:
                raise HTTPException(404, detail="trace not found")
            events = s.execute(select(AuditEvent).where(AuditEvent.trace_id == trace_id)
                               .order_by(AuditEvent.id)).scalars().all()
        root = next((x for x in spans if x.seq == 0), None)
        nodes = [x for x in spans if x.seq != 0]
        return {"trace_id": trace_id, "conversation_id": spans[0].conversation_id,
                "turn": _span(root) if root else None, "spans": [_span(x) for x in nodes],
                "cases": _case_ids(nodes),
                "audit_events": [{"type": e.type, "data": e.data, "at": e.at.isoformat()} for e in events]}
