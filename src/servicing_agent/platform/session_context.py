"""Conversation context that outlives a verified session: the payment evidence a borrower has shared.

Why: a borrower uploads a receipt, the session expires (or they verify again, or pick another loan),
then says "I already did the payment". Without this the agent asks for the UTR it already has.

What is kept, per conversation (the WhatsApp number), for ``ttl_seconds`` (default 24 h), as a short typed
log: ``payment`` (UTR, amount, date, time, payee, status, source, loan, last outcome, ticket), ``voice``
(screened, masked transcript snippet <= 200 chars), ``request`` (waiver/settlement submitted) and
``decision`` (policy clause + action). No images, no raw OCR text.

Guards against the obvious risks:
* **Re-checked, never trusted**: a remembered UTR is only an *input*. Every follow-up runs the live
  ledger (LMS) check again, so the answer reflects the ledger now, not a cached verdict.
* **Verified turns only**: the agent reads it only after verification (payment claims need a verified
  scope), so a different person holding the phone after the session expired learns nothing.
* **Loan-scoped**: evidence is re-checked against the loan verified *now*; a UTR on the borrower's other
  loan gets the wrong-loan answer, someone else's gets the usual neutral reply.
* **Bounded**: last ``max_items`` items, expires after ``ttl_seconds``; never sent to the audit log unmasked.

Redis when available (survives restarts, shared by workers); in-memory otherwise (tests, Redis down).
"""

from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger(__name__)

FIELDS = ("utr", "amount", "txn_date", "txn_time", "payee_vpa", "status", "source", "media_id")


class SessionContextStore:
    def __init__(self, redis_client=None, ttl_seconds: int = 24 * 3600, max_items: int = 12,
                 max_payments: int = 5):
        self.r = redis_client
        self.ttl = ttl_seconds
        self.max_items = max_items
        self.max_payments = max_payments
        self._mem: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_url(cls, redis_url: str, **kw) -> "SessionContextStore":
        try:
            import redis
            client = redis.from_url(redis_url, decode_responses=True)
            client.ping()
        except Exception as exc:  # noqa: BLE001
            log.warning("session context: Redis unavailable (%s); keeping context in memory", exc)
            client = None
        return cls(client, **kw)

    @staticmethod
    def _key(cid: str) -> str:
        return f"sa:ctx:{cid}"

    # ---- storage
    def _load(self, cid: str) -> list[dict]:
        if self.r is not None:
            try:
                raw = self.r.get(self._key(cid))
                return json.loads(raw) if raw else []
            except Exception:  # noqa: BLE001
                log.exception("session context read failed")
                return []
        with self._lock:
            exp, items = self._mem.get(cid, (0, []))
            return list(items) if exp > time.time() else []

    def _save(self, cid: str, items: list[dict]) -> None:
        items = items[: self.max_items]
        if self.r is not None:
            try:
                self.r.set(self._key(cid), json.dumps(items), ex=self.ttl)
                return
            except Exception:  # noqa: BLE001
                log.exception("session context write failed")
        with self._lock:
            self._mem[cid] = (time.time() + self.ttl, items)

    # ---- the log
    def remember(self, cid: str, kind: str, data: dict) -> None:
        """Add one entry (voice, request, decision...), newest first."""
        item = {"kind": kind, **{k: v for k, v in data.items() if v is not None}, "seen_at": time.time()}
        self._save(cid, [item] + self._load(cid))

    def window(self, cid: str, account_id: str | None = None, limit: int = 8) -> list[dict]:
        """What the model and reviewers see: masked, for this loan (or not tied to a loan), newest first."""
        out = []
        for it in self._load(cid):
            if account_id and it.get("account_id") not in (None, account_id):
                continue
            e = {"kind": it.get("kind", "payment"), "at": it.get("seen_at")}
            if e["kind"] == "payment":
                e.update(utr_last4=(it.get("utr") or "")[-4:], amount=it.get("amount"), date=it.get("txn_date"),
                         source=it.get("source"), outcome=it.get("last_result"), ticket=it.get("ticket_id"))
            else:
                e.update({k: v for k, v in it.items() if k not in ("kind", "seen_at", "account_id")})
            out.append({k: v for k, v in e.items() if v not in (None, "")})
            if len(out) >= limit:
                break
        return out

    # ---- payment evidence
    def remember_payment(self, cid: str, evidence: dict, account_id: str | None = None) -> None:
        """Add (or refresh) one piece of payment evidence. Only items with a UTR are kept."""
        utr = evidence.get("utr")
        if not utr:
            return
        items = self._load(cid)
        old = next((p for p in items if p.get("kind", "payment") == "payment" and p.get("utr") == utr), {})
        rest = [p for p in items if not (p.get("kind", "payment") == "payment" and p.get("utr") == utr)]
        item = {**old, "kind": "payment", **{k: evidence.get(k) for k in FIELDS if evidence.get(k) is not None},
                "seen_at": time.time()}
        if account_id:
            item["account_id"] = account_id
        # Keep at most max_payments payment entries.
        kept, n = [], 1
        for p in rest:
            if p.get("kind", "payment") == "payment":
                n += 1
                if n > self.max_payments:
                    continue
            kept.append(p)
        self._save(cid, [item] + kept)

    def record_outcome(self, cid: str, utr: str | None, account_id: str, result: str,
                       ticket_id: str | None = None) -> None:
        if not utr:
            return
        items = self._load(cid)
        for p in items:
            if p.get("kind", "payment") == "payment" and p.get("utr") == utr:
                p.update(account_id=account_id, last_result=result)
                if ticket_id:
                    p["ticket_id"] = ticket_id
        self._save(cid, items)

    def payments(self, cid: str) -> list[dict]:
        """Payment evidence, newest first."""
        return [p for p in self._load(cid) if p.get("kind", "payment") == "payment"]

    def latest_payment(self, cid: str) -> dict | None:
        items = self.payments(cid)
        return items[0] if items else None

    def forget(self, cid: str) -> None:
        if self.r is not None:
            try:
                self.r.delete(self._key(cid))
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._mem.pop(cid, None)
