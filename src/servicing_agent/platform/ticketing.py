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
