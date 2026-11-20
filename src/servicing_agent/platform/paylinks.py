"""Pay-link lifecycle + mocked payment (owner: T2).

A pay-now reply creates a DB-backed PayLink and returns a clickable URL to our own mock checkout
page (``{public_base}/pay/{link_id}``). When the borrower "pays" on that page we:

  1. mint a UTR + a D7 ``payment_transaction`` (success), and add it to the in-memory feed so the
     borrower can later claim it / fetch a receipt and it reconciles end to end;
  2. deduct the amount from the account's ``overdue_amount`` (floored at 0; DPD -> 0 when cleared);
  3. issue a receipt PDF;
  4. record a ``payment_received`` audit event and message the borrower on WhatsApp.

Persistence: paid links are replayed on startup (``replay_paid``) against the freshly-loaded store,
so the overdue deduction and the feed entry survive a restart. ``accounts.csv`` stays the immutable
base; paid links are the deltas.
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import select

from servicing_agent.contracts.events import EventType
from servicing_agent.platform.db import PayLink, PaymentTransaction, Receipt

logger = logging.getLogger(__name__)
_PAYEE_VPA = "collect@creditnirvana"


def _utr() -> str:
    return "".join(random.choice("0123456789") for _ in range(12))


class PayLinkService:
    # Keeps the MockPayLinks.create(account_id, amount) interface so the gateway and tests are happy.
    def __init__(self, session_factory, store, *, docs=None, channel=None, audit=None,
                 conversations=None, public_base_url: str = "http://localhost:8000"):
        self.Session = session_factory
        self.store = store
        self.docs = docs
        self.channel = channel
        self.audit = audit
        self.conversations = conversations
        self.base = public_base_url.rstrip("/")

    # ---- creation (called by the gateway on pay_now)
    def create(self, account_id: str, amount: float, conversation_id: str | None = None,
               meta: dict[str, Any] | None = None, expires_at: datetime | None = None) -> dict[str, Any]:
        link_id = uuid.uuid4().hex[:16]
        with self.Session.begin() as s:
            # One active link per account: supersede any earlier pending link so a stale amount can't be paid.
            for old in s.execute(select(PayLink).where(PayLink.account_id == account_id,
                                                       PayLink.status == "pending")).scalars():
                old.status = "superseded"
            s.add(PayLink(link_id=link_id, account_id=account_id, conversation_id=conversation_id,
                          amount=round(float(amount), 2), status="pending", created_at=datetime.utcnow(),
                          purpose=(meta or {}).get("purpose"), meta=meta or None, expires_at=expires_at))
        return {"payment_link": f"{self.base}/pay/{link_id}", "amount": round(float(amount), 2),
                "link_id": link_id}

    def list_pending(self) -> list[dict[str, Any]]:
        """Every pending link, newest first — the mock-payments console lists these to pay in real time."""
        with self.Session() as s:
            links = s.execute(select(PayLink).where(PayLink.status == "pending")
                              .order_by(PayLink.created_at.desc())).scalars().all()
        out = []
        for l in links:
            acc = self.store.accounts.get(l.account_id) or {}
            cust = self.store.customers.get(acc.get("customer_id", "")) or {}
            out.append({"link_id": l.link_id, "account_id": l.account_id, "name": cust.get("name"),
                        "amount": float(l.amount), "overdue": float(acc.get("overdue_amount") or 0),
                        "conversation_id": l.conversation_id,
                        "created_at": l.created_at.isoformat(timespec="seconds") if l.created_at else None})
        return out

    def create_and_pay(self, account_id: str, amount: float, conversation_id: str | None = None) -> dict[str, Any]:
        """Ad-hoc: create a link for an account and pay it immediately (the console's 'pay any account')."""
        link = self.create(account_id, amount, conversation_id)
        return {**self.pay(link["link_id"]), "link_id": link["link_id"], "account_id": account_id}

    def get(self, link_id: str) -> dict[str, Any] | None:
        with self.Session() as s:
            link = s.get(PayLink, link_id)
            if not link:
                return None
            return {"link_id": link.link_id, "account_id": link.account_id, "amount": float(link.amount),
                    "status": link.status, "utr": link.utr, "conversation_id": link.conversation_id,
                    "purpose": link.purpose, "meta": link.meta or {},
                    "expires_at": link.expires_at.isoformat(timespec="seconds") if link.expires_at else None,
                    "paid_at": link.paid_at.isoformat(timespec="seconds") if link.paid_at else None}

    # ---- the mocked payment
    def pay(self, link_id: str) -> dict[str, Any]:
        """Idempotent: only a `pending` link is charged; a repeat returns the existing result."""
        with self.Session.begin() as s:
            link = s.get(PayLink, link_id)
            if not link:
                return {"status": "not_found"}
            if link.status == "paid":
                return {"status": "already_paid", "amount": float(link.amount), "utr": link.utr}
            if link.status != "pending":
                return {"status": link.status, "amount": float(link.amount)}
            if link.expires_at and datetime.utcnow() > link.expires_at:
                link.status = "expired"                     # the same-day deadline passed (approved waiver)
                return {"status": "expired", "amount": float(link.amount)}
            account_id, amount = link.account_id, float(link.amount)
            purpose, meta = link.purpose, dict(link.meta or {})
            conv = link.conversation_id
            utr, txn_id, today = _utr(), f"MOCK{uuid.uuid4().hex[:10].upper()}", date.today().isoformat()
            link.status, link.utr, link.txn_id, link.paid_at = "paid", utr, txn_id, datetime.utcnow()
            s.add(PaymentTransaction(txn_id=txn_id, utr=utr, account_id=account_id, amount=amount,
                                     channel="UPI", status="success", txn_date=today, settled_date=today,
                                     payee_vpa=_PAYEE_VPA))

        overdue_after = self._apply(account_id, amount, utr, txn_id, today, waive=purpose == "approved_waiver")
        receipt = self._receipt(account_id, utr)
        self._audit(conv, {"amount": amount, "utr": utr, "overdue_after": overdue_after, "link_id": link_id,
                           "purpose": purpose, **({"case_id": meta.get("case_id"), "charges_waived": meta.get("charges")}
                                                  if purpose == "approved_waiver" else {})})
        self._notify(conv, amount, overdue_after, receipt)
        if purpose == "approved_waiver":
            self._say(conv, f"The charges of ₹{float(meta.get('charges') or 0):,.2f} are waived as approved "
                            f"(reference {meta.get('case_id')}). Your loan is now up to date. Thank you!")
        return {"status": "paid", "amount": amount, "utr": utr, "txn_id": txn_id,
                "overdue_after": overdue_after, "receipt": receipt}

    # ---- startup replay so deltas survive a restart
    def replay_paid(self) -> int:
        try:
            with self.Session() as s:
                links = s.execute(select(PayLink).where(PayLink.status == "paid")).scalars().all()
            if getattr(self.store, "persistent", False):
                return 0                    # Postgres-backed store: the deductions are already in the tables
            for l in links:
                when = (l.paid_at or l.created_at or datetime.utcnow()).date().isoformat()
                self._apply(l.account_id, float(l.amount), l.utr or _utr(), l.txn_id or "", when,
                            waive=l.purpose == "approved_waiver")
            if links:
                logger.info("PayLinks: replayed %d paid link(s) onto the store", len(links))
            return len(links)
        except Exception:  # noqa: BLE001 - never block startup
            logger.exception("PayLinks replay failed")
            return 0

    # ---- internals
    def _apply(self, account_id: str, amount: float, utr: str, txn_id: str, txn_date: str,
               waive: bool = False) -> float:
        """Deduct from overdue (floor 0; DPD->0 when cleared) and add the txn to the in-memory feed. For an
        approved waiver paid in time, the waived charges go too: overdue 0, charges 0, DPD 0."""
        acc = self.store.accounts.get(account_id)
        overdue_after = 0.0
        if acc is not None:
            overdue_after = 0.0 if waive else max(0.0, round(float(acc.get("overdue_amount") or 0) - amount, 2))
            fields = {"overdue_amount": overdue_after}
            if overdue_after == 0:
                fields["dpd"] = 0
                fields["dpd_band"] = "0-10"
            if waive:
                fields.update(bounce_charges=0.0, penal_charges=0.0)
            if hasattr(self.store, "update_account"):
                self.store.update_account(account_id, **fields)
            else:
                acc.update(fields)
        txn = {"txn_id": txn_id, "utr": utr, "account_id": account_id, "amount": amount,
               "channel": "UPI", "status": "success", "txn_date": txn_date,
               "settled_date": txn_date, "payee_vpa": _PAYEE_VPA}
        self.store.payments_by_utr[utr] = txn
        self.store.payments_by_account[account_id].append(txn)
        return overdue_after

    def _receipt(self, account_id: str, utr: str) -> dict | None:
        if not self.docs:
            return None
        try:
            rec = self.docs.receipt(account_id, utr)
            try:
                with self.Session.begin() as s:
                    s.add(Receipt(txn_id=utr, account_id=account_id, document_id=rec.get("document_id")))
            except Exception:  # noqa: BLE001 - receipt row is best-effort
                logger.exception("PayLinks: receipt row insert failed")
            return rec
        except Exception:  # noqa: BLE001
            logger.exception("PayLinks: receipt generation failed")
            return None

    def _audit(self, conv: str | None, data: dict) -> None:
        if self.audit and conv:
            try:
                self.audit(conv, [{"type": EventType.PAYMENT_RECEIVED.value, "data": data}])
            except Exception:  # noqa: BLE001
                logger.exception("PayLinks: audit failed")

    def expire_due(self) -> int:
        """Approved waivers not paid by their deadline: the link expires, the waiver is withdrawn (the charges
        stay on the overdue), and the borrower is told. Run every minute by the server."""
        now, expired = datetime.utcnow(), []
        with self.Session.begin() as s:
            for l in s.execute(select(PayLink).where(PayLink.status == "pending", PayLink.expires_at.is_not(None),
                                                     PayLink.expires_at < now)).scalars():
                l.status = "expired"
                expired.append((l.conversation_id, l.account_id, float(l.amount), dict(l.meta or {}), l.link_id))
        for conv, account_id, amount, meta, link_id in expired:
            acc = self.store.accounts.get(account_id) or {}
            self._audit(conv, {"event": "waiver_lapsed", "link_id": link_id, "case_id": meta.get("case_id"),
                               "charges": meta.get("charges")})
            self._say(conv, f"The payment of ₹{amount:,.2f} for your approved waiver (reference {meta.get('case_id')}) "
                            f"wasn't made today, so the waiver has been withdrawn. Your overdue amount stays "
                            f"₹{float(acc.get('overdue_amount') or 0):,.2f}, including the charges. You can still pay "
                            f"any amount towards it.")
        if expired:
            logger.info("PayLinks: %d approved-waiver link(s) expired", len(expired))
        return len(expired)

    def _say(self, conv: str | None, text: str) -> None:
        if not conv:
            return
        to = conv.split("wa:", 1)[-1] if conv.startswith("wa:") else conv
        if self.channel:
            try:
                self.channel.send_text(to, text)
            except Exception:  # noqa: BLE001
                logger.exception("PayLinks: borrower message failed")
        if self.conversations:
            try:
                c = self.conversations.load(conv)
                c.setdefault("transcript", []).append({"role": "agent", "text": text,
                                                       "at": datetime.utcnow().isoformat(timespec="seconds")})
                self.conversations.persist(conv)
            except Exception:  # noqa: BLE001
                logger.exception("PayLinks: transcript append failed")

    def _notify(self, conv: str | None, amount: float, overdue_after: float, receipt: dict | None) -> None:
        """Confirm on WhatsApp + drop the confirmation into the transcript so it shows in Conversations."""
        if not conv:
            return
        msg = (f"Payment of ₹{amount:,.2f} received ✓ Thank you. "
               f"Your overdue is now ₹{overdue_after:,.2f}.")
        to = conv.split("wa:", 1)[-1] if conv.startswith("wa:") else conv
        if self.channel:
            try:
                self.channel.send_text(to, msg)
                if receipt and receipt.get("path") and hasattr(self.channel, "send_document"):
                    self.channel.send_document(to, receipt["path"], caption="Payment receipt")
            except Exception:  # noqa: BLE001
                logger.exception("PayLinks: borrower notify failed")
        if self.conversations:
            try:
                c = self.conversations.load(conv)
                now = datetime.utcnow().isoformat(timespec="seconds")
                c.setdefault("transcript", []).append({"role": "agent", "text": msg, "at": now})
                c.setdefault("events", []).append(
                    {"type": EventType.PAYMENT_RECEIVED.value, "at": now,
                     "data": {"amount": amount, "overdue_after": overdue_after}})
                self.conversations.persist(conv)
            except Exception:  # noqa: BLE001
                logger.exception("PayLinks: transcript append failed")
