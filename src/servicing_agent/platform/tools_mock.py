"""Mock implementations of contracts/tools.py over the synthetic DataStore (owner: T2)."""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from servicing_agent.config import routing_matrix
from servicing_agent.data.identifiers import normalize_mobile
from servicing_agent.data.store import DataStore


class MockLMS:
    def __init__(self, store: DataStore):
        self.s = store

    def account_exists(self, account_id: str) -> bool:
        return account_id in self.s.accounts

    def same_borrower(self, contact_id: str, other_account_id: str) -> bool:
        """Is the verified contact's mobile also an active contact on this other loan?"""
        c = self.s.contacts.get(contact_id)
        if not c:
            return False
        for link in self.s.links_by_account.get(other_account_id, []):
            o = self.s.contacts.get(link["contact_id"])
            if o and o.get("is_active") and o.get("mobile") == c.get("mobile"):
                return True
        return False

    def active_contact_for(self, account_id: str, mobile: str) -> dict[str, Any] | None:
        mobile = normalize_mobile(mobile)
        for link in self.s.links_by_account.get(account_id, []):
            ct = self.s.contacts.get(link["contact_id"])
            if ct and ct["is_active"] and ct["mobile"] == mobile:
                return {"contact_id": ct["contact_id"], "customer_id": ct["customer_id"],
                        "role": link["role"], "added_on": ct["added_on"]}
        return None

    def dues(self, account_id: str) -> dict[str, Any]:
        a = self.s.accounts[account_id]
        return {"emi_amount": a["emi_amount"], "next_due_date": a["next_due_date"],
                "overdue_amount": a["overdue_amount"], "dpd": a["dpd"]}

    def foreclosure_quote(self, account_id: str) -> dict[str, Any]:
        a = self.s.accounts[account_id]
        fc = round(a["principal_outstanding"] * a["foreclosure_charge_pct"] / 100, 2)
        breakup = {
            "principal": a["principal_outstanding"],
            "overdue": a["overdue_amount"],
            "charges": round(a["bounce_charges"] + a["penal_charges"], 2),
            "foreclosure_fee": fc,
        }
        return {"amount": round(sum(breakup.values()), 2),
                "valid_until": (date.today() + timedelta(days=3)).isoformat(),
                "breakup": breakup}

    def loan_position(self, account_id: str) -> dict[str, Any]:
        a = self.s.accounts[account_id]
        keys = ["product", "principal_outstanding", "overdue_amount", "dpd", "emi_amount", "tenure_left",
                "bounce_charges", "penal_charges", "interest_rate", "next_due_date",
                "sanction_amount", "tenure_months", "status"]
        out = {k: a[k] for k in keys if k in a}
        out["foreclosure_amount_today"] = self.foreclosure_quote(account_id)["amount"]
        return out


class MockPaymentFeed:
    def __init__(self, store: DataStore):
        self.s = store
        self._claimed: dict[str, str] = {}

    def find_by_utr(self, utr: str) -> dict[str, Any] | None:
        return self.s.payments_by_utr.get(utr)

    def find_candidates(self, account_id: str, amount: float, date_iso: str, days: int = 1) -> list[dict[str, Any]]:
        d = date.fromisoformat(date_iso[:10])
        return [p for p in self.s.payments_by_account.get(account_id, [])
                if abs(p["amount"] - amount) <= 0.5 and abs((date.fromisoformat(p["txn_date"]) - d).days) <= days]

    def history(self, account_id: str, months: int = 12) -> list[dict[str, Any]]:
        cutoff = date.today() - timedelta(days=30 * months)
        return sorted((p for p in self.s.payments_by_account.get(account_id, [])
                       if date.fromisoformat(p["txn_date"]) >= cutoff), key=lambda p: p["txn_date"])

    # With a ClaimLedger attached (the runtime does this), "claimed" comes from payment_claims in the
    # database and survives restarts. Without one (bare unit tests) it falls back to memory.
    ledger = None

    def claimed_by(self, utr: str) -> str | None:
        return self.ledger.confirmed_by(utr) if self.ledger else self._claimed.get(utr)

    def is_claimed(self, utr: str) -> bool:
        return self.claimed_by(utr) is not None

    def mark_claimed(self, utr: str, account_id: str) -> None:
        if not self.ledger:                 # the ledger records confirmations itself
            self._claimed[utr] = account_id


class MockOTP:
    def __init__(self, stub_code: str):
        self.stub_code = stub_code
        self.sent: dict[str, str] = {}

    def send(self, mobile: str, account_id: str | None = None) -> str:
        rid = f"otp_{uuid.uuid4().hex[:10]}"
        self.sent[rid] = normalize_mobile(mobile)
        return rid

    def verify(self, request_id: str, code: str) -> bool:
        return request_id in self.sent and code == self.stub_code


class MockDocs:
    def __init__(self, store: DataStore):
        self.s = store

    def receipt(self, account_id: str, utr: str) -> dict[str, Any]:
        p = self.s.payments_by_utr[utr]
        assert p["account_id"] == account_id
        return {"document_id": f"RCPT-{utr}", "document_type": "receipt",
                "payment_date": p["txn_date"], "amount": p["amount"], "utr": utr,
                "password_hint": "DOB as DDMMYYYY"}

    def statement(self, account_id: str, period_from: str, period_to: str) -> dict[str, Any]:
        return {"document_id": f"STMT-{account_id[-4:]}-{period_from}-{period_to}", "document_type": "statement",
                "period_from": period_from, "period_to": period_to, "password_hint": "DOB as DDMMYYYY"}


class MockPayLinks:
    def create(self, account_id: str, amount: float) -> dict[str, Any]:
        return {"payment_link": f"https://pay.example.test/l/{uuid.uuid4().hex[:12]}", "amount": amount}


@dataclass
class MockTicketing:
    tickets: list[dict[str, Any]] = field(default_factory=list)
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))

    def create(self, request_type: str, summary: dict[str, Any], conversation_id: str,
               account_id: str | None = None) -> dict[str, Any]:
        route = routing_matrix().get(request_type) or routing_matrix()["unknown"]
        t = {"ticket_id": f"TKT{next(self._seq):06d}", "request_type": request_type, "queue": route["queue"],
             "priority": route["priority"],
             "sla_due": (datetime.utcnow() + timedelta(hours=route["sla_hours"])).isoformat(timespec="seconds"),
             "conversation_id": conversation_id, "account_id": account_id, "summary": summary}
        self.tickets.append(t)
        return t


@dataclass
class MockToolSet:
    lms: MockLMS
    payments: MockPaymentFeed
    otp: MockOTP
    docs: MockDocs
    paylinks: MockPayLinks
    tickets: MockTicketing

    @classmethod
    def from_store(cls, store: DataStore, otp_stub_code: str) -> "MockToolSet":
        return cls(MockLMS(store), MockPaymentFeed(store), MockOTP(otp_stub_code), MockDocs(store),
                   MockPayLinks(), MockTicketing())
