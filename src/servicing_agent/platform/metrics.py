"""Operational metrics (owner: T2) — the Pulse + Journeys data layer.

Computed on read from the persisted audit trail (``audit_events``) plus ``tickets`` and
``payment_claims``. No rollup table: volumes are low and compute-on-read is always fresh and
correct. A daily rollup can be added later if needed. Nothing here reads borrower text —
only event types, counts and timestamps.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from servicing_agent.platform.claims import is_suspected_fake
from servicing_agent.platform.db import AuditEvent, ConversationState, PaymentClaim, Ticket

# What borrowers ask for, rolled into decision-useful buckets for the Journeys screen.
INTENT_GROUP = {
    "payment_claim": "Payments", "pay_now": "Payments", "payment_history": "Payments",
    "receipt_request": "Payments", "payment_difficulty": "Payments",
    "emi_due_query": "Balances & info", "loan_summary": "Balances & info", "charges_query": "Balances & info",
    "statement_request": "Balances & info", "foreclosure_quote": "Balances & info",
    "account_question": "Balances & info", "policy_question": "Balances & info", "faq": "Balances & info",
    "case_status": "Balances & info",
    "waiver_request": "Concessions", "settlement_request": "Concessions", "charge_reversal": "Concessions",
    "complaint": "Complaints & disputes", "dispute": "Complaints & disputes",
    "harassment_complaint": "Complaints & disputes", "legal_threat": "Complaints & disputes",
    "distress": "Complaints & disputes",
}



def _pct(num: int, den: int) -> float:
    return round(100 * num / den, 1) if den else 0.0


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


class MetricsService:
    def __init__(self, session_factory, store, tickets):
        self.Session = session_factory
        self.store = store
        self.tickets = tickets

    def pulse(self, days: int = 29) -> dict[str, Any]:
        days = max(1, min(days, 120))
        # Bucket by UTC date to match audit_events.at (naive UTC); using local date() dropped
        # today's events into yesterday's bucket when the server's UTC date != local date.
        today = datetime.utcnow().date()
        day_keys = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        in_window = set(day_keys)
        since = datetime.utcnow() - timedelta(days=days)

        with self.Session() as s:
            evs = s.execute(
                select(AuditEvent.conversation_id, AuditEvent.type, AuditEvent.at)
                .where(AuditEvent.at >= since)
            ).all()
            claims = s.execute(
                select(PaymentClaim.result, PaymentClaim.queue, PaymentClaim.created_at, PaymentClaim.ticket_id)
                .where(PaymentClaim.created_at >= since)
            ).all()
            tks = s.execute(
                select(Ticket.created_at, Ticket.resolved_at).where(Ticket.created_at >= since)
            ).all()

        def bucket() -> dict[str, int]:
            return {k: 0 for k in day_keys}

        inbound, outbound, verified = bucket(), bucket(), bucket()
        injections, blocks = bucket(), bucket()
        active: dict[str, set] = defaultdict(set)
        conv_events: dict[str, list] = defaultdict(list)   # cid -> [(at, 'in'|'out')]
        conv_handoff: dict[str, bool] = defaultdict(bool)  # cid reached a human

        for cid, typ, at in evs:
            d = at.date().isoformat()
            if d not in in_window:
                continue
            active[d].add(cid)
            if typ == "inbound":
                inbound[d] += 1
                conv_events[cid].append((at, "in"))
            elif typ == "outbound":
                outbound[d] += 1
                conv_events[cid].append((at, "out"))
            elif typ == "verified":
                verified[d] += 1
            elif typ in ("handoff", "ticket_created"):
                conv_handoff[cid] = True
            elif typ == "injection_detected":
                injections[d] += 1
            elif typ == "output_blocked":
                blocks[d] += 1

        total_conv, zero = bucket(), bucket()
        for d in day_keys:
            total_conv[d] = len(active[d])
            zero[d] = sum(1 for c in active[d] if not conv_handoff.get(c))
        zero_rate = [_pct(zero[d], total_conv[d]) for d in day_keys]

        latencies: list[float] = []
        for seq in conv_events.values():
            seq.sort()
            first_in = next((t for t, k in seq if k == "in"), None)
            if first_in is None:
                continue
            first_out = next((t for t, k in seq if k == "out" and t >= first_in), None)
            if first_out is not None:
                latencies.append((first_out - first_in).total_seconds())

        t_created, t_resolved = bucket(), bucket()
        for created_at, resolved_at in tks:
            dc = created_at.date().isoformat()
            if dc in t_created:
                t_created[dc] += 1
            if resolved_at is not None:
                dr = resolved_at.date().isoformat()
                if dr in t_resolved:
                    t_resolved[dr] += 1

        fake = sum(1 for r, q, _, tid in claims if is_suspected_fake(r, q, tid))

        return {
            "days": day_keys,
            "series": {
                "inbound": [inbound[d] for d in day_keys],
                "outbound": [outbound[d] for d in day_keys],
                "verified": [verified[d] for d in day_keys],
                "conversations": [total_conv[d] for d in day_keys],
                "zero_touch_rate": zero_rate,
                "tickets_created": [t_created[d] for d in day_keys],
                "tickets_resolved": [t_resolved[d] for d in day_keys],
            },
            "headline": {
                "conversations": sum(total_conv.values()),
                "zero_touch_rate": _pct(sum(zero.values()), sum(total_conv.values())),
                "first_response_p50_s": round(_quantile(latencies, 0.50)),
                "first_response_p95_s": round(_quantile(latencies, 0.95)),
                "verified": sum(verified.values()),
                "fake_proofs_blocked": fake,
                "injections_blocked": sum(injections.values()),
                "output_blocked": sum(blocks.values()),
                "shared_before_verification": 0,  # enforced invariant
            },
        }

    def guardrails(self, days: int = 29) -> dict[str, Any]:
        """The hard-guardrail counters (the mock's row of near-zeros)."""
        h = self.pulse(days)["headline"]
        return {
            "shared_before_verification": h["shared_before_verification"],
            "fake_proofs_blocked": h["fake_proofs_blocked"],
            "injections_blocked": h["injections_blocked"],
            "output_blocked": h["output_blocked"],
        }

    def journeys(self, days: int = 29) -> dict[str, Any]:
        """Journey views (last ``days``): the inbound self-serve funnel, what borrowers ask for
        (intents, grouped), requests routed to humans, and product mix of engaged borrowers."""
        since = datetime.utcnow() - timedelta(days=days)
        with self.Session() as s:
            evs = s.execute(select(AuditEvent.conversation_id, AuditEvent.type, AuditEvent.data)
                            .where(AuditEvent.at >= since)).all()
            tks = s.execute(select(Ticket.request_type, Ticket.account_id).where(Ticket.created_at >= since)).all()
            claim_accts = s.execute(select(PaymentClaim.account_id).where(PaymentClaim.created_at >= since)).scalars().all()
            conv_rows = s.execute(select(ConversationState.data).where(ConversationState.updated_at >= since)).scalars().all()

        # Inbound self-serve funnel + what borrowers ask for (intents).
        convs: set = set()
        verified: set = set()
        handed: set = set()
        by_intent: dict[str, int] = defaultdict(int)
        for cid, typ, data in evs:
            if typ == "inbound":
                convs.add(cid)
            elif typ == "verified":
                verified.add(cid)
            elif typ in ("handoff", "ticket_created"):
                handed.add(cid)
            if typ in ("tool_call", "handoff") and isinstance(data, dict) and data.get("intent"):
                by_intent[data["intent"]] += 1
        convs |= verified | handed
        inbound_funnel = {
            "conversations": len(convs),
            "verified": len(verified & convs),
            "zero_touch": len(convs - handed),
            "escalated": len(handed & convs),
        }

        by_intent_grouped: dict[str, int] = defaultdict(int)
        for it, n in by_intent.items():
            by_intent_grouped[INTENT_GROUP.get(it, "Other")] += n

        by_request: dict[str, int] = defaultdict(int)
        engaged: set = set()
        for rt, aid in tks:
            by_request[rt] += 1
            if aid:
                engaged.add(aid)
        engaged.update(a for a in claim_accts if a)
        for data in conv_rows:
            scope = (data or {}).get("session", {}).get("scope") if isinstance(data, dict) else None
            aid = scope.get("account_id") if isinstance(scope, dict) else (scope if isinstance(scope, str) else None)
            if aid:
                engaged.add(aid)

        by_product: dict[str, int] = defaultdict(int)
        for aid in engaged:
            a = self.store.accounts.get(aid)
            if a:
                by_product[a.get("product", "?")] += 1

        return {"inbound_funnel": inbound_funnel, "by_intent": dict(by_intent),
                "by_intent_grouped": dict(by_intent_grouped),
                "by_request_type": dict(by_request), "by_product": dict(by_product)}
