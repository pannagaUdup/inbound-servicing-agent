"""DB-backed ticketing (owner: T2).

Replaces the in-memory MockTicketing: every routed request becomes a row in the
``tickets`` table (survives restarts, visible to the ops console). A ticket is routed
to a queue + SLA + priority from ``config/routing_matrix.yaml`` and, for now, assigned
to a single ops user (``settings.ops_assignee``) — the one-queue model.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from servicing_agent.config import routing_matrix
from servicing_agent.platform.db import Ticket

# Routing queue -> ops group (the Handoff desk's lanes).
QUEUE_GROUP = {
    "servicing_disputes": "Disputes", "complaints": "Complaints", "servicing_charges": "Charges",
    "waiver_approvals": "Approvals", "settlement_approvals": "Approvals",
    "payment_reconciliation": "Payments", "fraud_review": "Fraud", "priority_care": "Care",
    "legal": "Legal", "grievance_officer": "Grievance", "security": "Security", "servicing_general": "General",
    "collections": "Collections",
}
# Curated next action per request type (the mock's per-ticket "next action" line).
NEXT_ACTION = {
    "dispute": "Review dispute evidence and respond within SLA.",
    "complaint": "Acknowledge and investigate the complaint.",
    "charge_reversal": "Verify the charge and reverse it if valid.",
    "payment_unmatched": "Reconcile the UTR against the payment feed.",
    "payment_mismatch": "Fraud review: check the proof for tampering before any contact.",
    "distress": "Call the borrower; offer hardship support.",
    "legal_threat": "Escalate to legal; do not respond on the merits.",
    "harassment_complaint": "Escalate to the grievance officer; pause recovery contact.",
    "waiver_request": "Route into the approvals workflow.",
    "settlement_request": "Route into the approvals workflow.",
    "security_alert": "Investigate the security signal.",
}
# Borrower-facing one-liner sent when a ticket is resolved (by request type).
RESOLVED_LINE = {
    "dispute": "We've reviewed your dispute and updated your account accordingly.",
    "complaint": "Your complaint has been addressed — thank you for your patience.",
    "charge_reversal": "The charge has been reviewed and reversed where applicable.",
    "harassment_complaint": "We've acted on your complaint and taken the necessary steps.",
    "payment_unmatched": "Your payment has been reconciled.",
    "waiver_request": "Your waiver request has been actioned.",
    "settlement_request": "Your settlement request has been actioned.",
    "partial_payment_waiver": "Your part-payment request has been actioned.",
    "distress": "Our care team has followed up on your request.",
    "legal_threat": "Our team has reviewed and responded to your request.",
    "borrower_escalation": "Your escalation has been resolved.",
}
# Internal/fraud/security tickets: don't message the borrower on resolution.
RESOLVE_NOTIFY_SUPPRESS = {"payment_mismatch", "security_alert", "sim_swap_review"}


class TicketService:
    def __init__(self, session_factory, assignee: str | None = None):
        self.Session = session_factory
        self.assignee = assignee

    def create(self, request_type: str, summary: dict[str, Any], conversation_id: str,
               account_id: str | None = None) -> dict[str, Any]:
        route = routing_matrix().get(request_type) or routing_matrix()["unknown"]
        now = datetime.utcnow()
        with self.Session.begin() as s:
            t = Ticket(
                conversation_id=conversation_id, account_id=account_id,
                request_type=request_type, routing_queue=route["queue"], priority=route["priority"],
                sla_hours=route["sla_hours"], summary=summary, source=summary.get("source"),
                status="open", assignee=self.assignee, created_at=now,
            )
            s.add(t)
            s.flush()                       # assigns the autoincrement id
            t.ticket_id = f"TKT{t.id:06d}"  # stable human id derived from the row id
            s.flush()
            return self._out(t, now)

    def list(self, assignee: str | None = None, queue: str | None = None,
             status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        stmt = select(Ticket).order_by(Ticket.created_at.desc()).limit(limit)
        if assignee is not None:
            stmt = stmt.where(Ticket.assignee == assignee)
        if queue is not None:
            stmt = stmt.where(Ticket.routing_queue == queue)
        if status is not None:
            stmt = stmt.where(Ticket.status == status)
        with self.Session() as s:
            return [self._out(t) for t in s.execute(stmt).scalars().all()]

    def open_for_account(self, account_id: str, request_type: str) -> dict[str, Any] | None:
        with self.Session() as s:
            t = s.scalar(select(Ticket).where(Ticket.account_id == account_id, Ticket.request_type == request_type,
                                              Ticket.status == "open").order_by(Ticket.created_at.desc()).limit(1))
            return self._out(t) if t else None

    def status_of(self, ticket_id: str) -> str | None:
        with self.Session() as s:
            return s.scalar(select(Ticket.status).where(Ticket.ticket_id == ticket_id))

    def get(self, ticket_id: str) -> dict[str, Any] | None:
        with self.Session() as s:
            t = s.scalar(select(Ticket).where(Ticket.ticket_id == ticket_id))
            return self._out(t) if t else None

    def supersede_for_case(self, account_id: str, case_id: str, note: str) -> list[dict[str, Any]]:
        """Close the open tickets of a case whose request was replaced; reviewers see why."""
        out = []
        with self.Session.begin() as s:
            for t in s.scalars(select(Ticket).where(Ticket.account_id == account_id, Ticket.status == "open")):
                if (t.summary or {}).get("case_id") == case_id:
                    t.status = "superseded"           # not "resolved": the KPI charts stay honest
                    t.summary = {**(t.summary or {}), "superseded_note": note}
                    out.append(self._out(t))
        return out

    def attach_context(self, ticket_id: str, bag: dict) -> bool:
        """Persist the escalation context bag onto a ticket after it was created (the bag is built
        later in the turn). Reassigns the JSON column so SQLAlchemy flushes the change."""
        with self.Session.begin() as s:
            t = s.execute(select(Ticket).where(Ticket.ticket_id == ticket_id)).scalar_one_or_none()
            if not t:
                return False
            t.summary = {**(t.summary or {}), "context_bag": bag}
            return True

    def resolve(self, ticket_id: str) -> bool:
        with self.Session.begin() as s:
            res = s.execute(
                update(Ticket).where(Ticket.ticket_id == ticket_id, Ticket.status == "open")
                .values(status="resolved", resolved_at=datetime.utcnow())
            )
            return res.rowcount == 1

    @staticmethod
    def _out(t: Ticket, created_at: datetime | None = None) -> dict[str, Any]:
        created = created_at or t.created_at
        sla_due = (created + timedelta(hours=t.sla_hours)).isoformat(timespec="seconds") if t.sla_hours else None
        return {
            "ticket_id": t.ticket_id, "request_type": t.request_type, "queue": t.routing_queue,
            "group": QUEUE_GROUP.get(t.routing_queue, "General"),
            "next_action": NEXT_ACTION.get(t.request_type, "Review and action."),
            "priority": t.priority, "sla_hours": t.sla_hours, "sla_due": sla_due,
            "conversation_id": t.conversation_id, "account_id": t.account_id,
            "status": t.status, "assignee": t.assignee, "summary": t.summary, "source": t.source,
            "created_at": created.isoformat(timespec="seconds") if created else None,
            "resolved_at": t.resolved_at.isoformat(timespec="seconds") if t.resolved_at else None,
        }
