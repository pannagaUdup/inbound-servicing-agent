"""Collection-agency registry + complaints attribution (owner: T2).

Third-party agencies handle on-ground recovery. Each account is deterministically assigned to
one agency (stable hash of account_id), so a complaint about an account — harassment, dispute,
or a service complaint — is attributed to the agency responsible for it. This powers the mock's
Complaints → "collection agencies" view.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import select

from servicing_agent.platform.db import CollectionAgency

# Complaint-style ticket request types that attribute to an agency.
COMPLAINT_TYPES = ("complaint", "dispute", "harassment_complaint", "charge_reversal")

_SEED = [
    ("AG-NORTH", "Sentinel Recovery Services", "North", "1800-200-1101"),
    ("AG-SOUTH", "Dakshin Collections Pvt Ltd", "South", "1800-200-1102"),
    ("AG-EAST", "Orient Credit Solutions", "East", "1800-200-1103"),
    ("AG-WEST", "Paschim Asset Recovery", "West", "1800-200-1104"),
    ("AG-CENTRAL", "Madhya Recovery Associates", "Central", "1800-200-1105"),
    ("AG-METRO", "Metro Field Collections", "Metro", "1800-200-1106"),
]


def seed_agencies(session_factory) -> None:
    """Idempotent: insert the standard agency set if the table is empty."""
    with session_factory.begin() as s:
        existing = set(s.execute(select(CollectionAgency.agency_id)).scalars().all())
        for aid, name, region, phone in _SEED:
            if aid not in existing:
                s.add(CollectionAgency(agency_id=aid, name=name, region=region, phone=phone, active=True))


class AgencyService:
    def __init__(self, session_factory, store, tickets, conversations=None):
        self.Session = session_factory
        self.store = store
        self.tickets = tickets
        self.conversations = conversations  # to recover the account from a verified conversation

    def _account_for(self, t: dict) -> str | None:
        """The ticket's account, or — for complaints raised before/without verification — the
        account from the conversation's verified scope, so it still attributes to an agency."""
        if t.get("account_id"):
            return t["account_id"]
        cid = t.get("conversation_id")
        if cid and self.conversations is not None:
            convo = self.conversations.get(cid)
            scope = (convo or {}).get("session", {}).get("scope") or {}
            return scope.get("account_id")
        return None

    def _agencies(self) -> list[dict[str, Any]]:
        with self.Session() as s:
            rows = s.execute(select(CollectionAgency).order_by(CollectionAgency.agency_id)).scalars().all()
        return [{"agency_id": a.agency_id, "name": a.name, "region": a.region,
                 "phone": a.phone, "active": a.active} for a in rows]

    def agency_for(self, account_id: str | None, agency_ids: list[str]) -> str | None:
        if not account_id or not agency_ids:
            return None
        idx = int(hashlib.md5(account_id.encode()).hexdigest(), 16) % len(agency_ids)
        return agency_ids[idx]

    def _borrower_name(self, account_id: str | None) -> str | None:
        if not account_id:
            return None
        acc = self.store.accounts.get(account_id)
        cust = self.store.customers.get(acc.get("customer_id", "")) if acc else None
        return cust.get("name") if cust else None

    def complaints(self, status: str | None = "open") -> list[dict[str, Any]]:
        agencies = self._agencies()
        by_id = {a["agency_id"]: a for a in agencies}
        ids = [a["agency_id"] for a in agencies]
        out = []
        for t in self.tickets.list(status=status, limit=500):
            if t["request_type"] not in COMPLAINT_TYPES:
                continue
            account_id = self._account_for(t)
            aid = self.agency_for(account_id, ids)
            out.append({**t, "account_id": account_id, "borrower_name": self._borrower_name(account_id),
                        "agency_id": aid, "agency_name": by_id.get(aid, {}).get("name") if aid else None})
        return out

    def list_agencies(self) -> list[dict[str, Any]]:
        agencies = self._agencies()
        ids = [a["agency_id"] for a in agencies]
        # accounts assigned per agency (static book)
        assigned: dict[str, int] = {a: 0 for a in ids}
        for account_id in self.store.accounts:
            aid = self.agency_for(account_id, ids)
            if aid:
                assigned[aid] += 1
        # open complaints per agency + SLA breaches
        open_cnt: dict[str, int] = {a: 0 for a in ids}
        breached: dict[str, int] = {a: 0 for a in ids}
        last_at: dict[str, str | None] = {a: None for a in ids}
        now = datetime.utcnow().isoformat(timespec="seconds")
        for t in self.tickets.list(status="open", limit=500):
            if t["request_type"] not in COMPLAINT_TYPES:
                continue
            aid = self.agency_for(self._account_for(t), ids)
            if not aid:
                continue
            open_cnt[aid] += 1
            if t.get("sla_due") and t["sla_due"] < now:
                breached[aid] += 1
            if t.get("created_at") and (last_at[aid] is None or t["created_at"] > last_at[aid]):
                last_at[aid] = t["created_at"]
        for a in agencies:
            i = a["agency_id"]
            a.update(accounts=assigned[i], open_complaints=open_cnt[i],
                     sla_breached=breached[i], last_complaint_at=last_at[i])
        agencies.sort(key=lambda a: a["open_complaints"], reverse=True)
        return agencies
