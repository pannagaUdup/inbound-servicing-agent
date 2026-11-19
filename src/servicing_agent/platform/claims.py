"""Payment-claim ledger: the audit record of every claim and the replay guard (owner: T2).

One row per (account, UTR) in ``payment_claims``:

* first claim            -> insert (attempts=1, verdict, source, message id, ticket id)
* the same UTR again     -> update the same row (attempts+1, last_message_id, latest verdict);
                            the ticket id is kept, so a resend never opens a second ticket
* a confirmed (MATCHED) row is final: the UTR counts as paid by that account from then on

Survives restarts (unlike the old in-memory set), so a confirmed UTR can't be accepted again
for another account after a redeploy. Receipts are recorded once per D7 transaction.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from servicing_agent.platform.db import PaymentClaim, Receipt

CONFIRMED = "MATCHED"


def _row(c: PaymentClaim) -> dict:
    return {
        "id": c.id, "account_id": c.account_id, "utr": c.utr, "result": c.result, "source": c.source,
        "ticket_id": c.ticket_id, "queue": c.queue, "attempts": c.attempts, "matched_txn_id": c.matched_txn_id,
        "first_message_id": c.first_message_id, "last_message_id": c.last_message_id,
        "confirmed_at": c.confirmed_at.isoformat() if c.confirmed_at else None,
        "amount": float(c.amount) if c.amount is not None else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


class ClaimLedger:
    def __init__(self, session_factory):
        self.Session = session_factory

    def get(self, account_id: str, utr: str | None) -> dict | None:
        if not utr:
            return None
        with self.Session() as s:
            c = s.scalar(select(PaymentClaim).where(PaymentClaim.account_id == account_id, PaymentClaim.utr == utr))
            return _row(c) if c else None

    def latest(self, conversation_id: str, account_id: str, since: datetime) -> dict | None:
        """The most recent claim in this conversation for this loan (for "I already sent it" follow-ups)."""
        with self.Session() as s:
            c = s.scalar(select(PaymentClaim).where(PaymentClaim.conversation_id == conversation_id,
                                                    PaymentClaim.account_id == account_id,
                                                    PaymentClaim.updated_at >= since)
                         .order_by(PaymentClaim.updated_at.desc()).limit(1))
            return _row(c) if c else None

    def confirmed_by(self, utr: str) -> str | None:
        """Account that has this UTR confirmed as paid, if any."""
        with self.Session() as s:
            return s.scalar(select(PaymentClaim.account_id).where(PaymentClaim.utr == utr,
                                                                  PaymentClaim.result == CONFIRMED))

    def record(self, *, conversation_id: str, account_id: str, utr: str | None, result: str, source: str,
               message_id: str | None, amount: float | None = None, txn_date: str | None = None,
               payee_vpa: str | None = None, media_id: str | None = None, matched_txn_id: str | None = None,
               reasons: list | None = None, ticket_id: str | None = None, queue: str | None = None) -> dict:
        """Insert the first claim for (account, UTR), or update it on a resend. Returns the row."""
        now = datetime.utcnow()
        try:
            paid_at = datetime.fromisoformat(txn_date[:10]) if txn_date else None
        except ValueError:
            paid_at = None
        for _ in range(2):                                  # retry once if a concurrent insert won the race
            try:
                with self.Session.begin() as s:
                    c = None
                    if utr:
                        c = s.scalar(select(PaymentClaim).where(PaymentClaim.account_id == account_id,
                                                                PaymentClaim.utr == utr).with_for_update())
                    if c is None:
                        c = PaymentClaim(conversation_id=conversation_id, account_id=account_id, utr=utr,
                                         source=source, media_id=media_id, amount=amount, paid_at=paid_at,
                                         payee_vpa=payee_vpa, attempts=1, first_message_id=message_id,
                                         created_at=now)
                        s.add(c)
                    else:
                        c.attempts = (c.attempts or 1) + 1
                    # A confirmed claim stays confirmed; later sends only add to the audit counters.
                    if c.result != CONFIRMED:
                        c.result = result
                        c.reasons = reasons
                        c.queue = queue or c.queue
                        c.matched_txn_id = matched_txn_id or c.matched_txn_id
                    if result == CONFIRMED and c.confirmed_at is None:
                        c.confirmed_at = now
                    c.ticket_id = c.ticket_id or ticket_id
                    c.last_message_id = message_id
                    c.updated_at = now
                    s.flush()
                    return _row(c)
            except IntegrityError:
                continue
        raise RuntimeError(f"could not record claim for {account_id}/{utr}")

    def record_receipt(self, txn_id: str, account_id: str, document_id: str | None) -> bool:
        """True if this is the first receipt for the transaction; False if it already existed."""
        try:
            with self.Session.begin() as s:
                if s.scalar(select(Receipt.id).where(Receipt.txn_id == txn_id)):
                    return False
                s.add(Receipt(txn_id=txn_id, account_id=account_id, document_id=document_id))
                return True
        except IntegrityError:
            return False
