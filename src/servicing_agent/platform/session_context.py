"""The context window of one verified session.

A session starts when the borrower verifies (OTP) for a loan and ends when it expires, is invalidated,
or the borrower verifies again / switches loan. Each session gets its own, empty window: nothing said in an
earlier session is carried into a new one.

What is kept, per session, as a short typed log (newest first, at most ``max_items``): ``voice`` (screened,
masked transcript snippet <= 200 chars), ``payment`` (masked: UTR last 4, amount, date of a receipt shared in
this session), ``request`` (waiver/settlement submitted), ``decision`` (policy clause + action) and
``escalation``. No images, no raw OCR text, no full UTRs.

Used for: the reply writer (so it can acknowledge what was said a moment ago) and the context bag on tickets
raised in this session.

What is *not* kept here: payment evidence across sessions. An unresolved payment claim ("I already paid" after
the session expired) is read from the payment-claims ledger for the verified customer and loan (CTX-01), which
already records every claim, and is re-checked against the ledger then.

Redis when available (survives restarts, shared by workers); in-memory otherwise (tests, Redis down). Entries
expire after ``ttl_seconds`` as a backstop; a session's window is also deleted when the session expires.
"""

from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger(__name__)


def session_key(conversation_id: str, scope) -> str | None:
    """One key per verified session: the conversation, the loan and the moment it was verified."""
    if scope is None:
        return None
    return f"{conversation_id}:{scope.account_id}:{scope.verified_at.strftime('%Y%m%d%H%M%S%f')}"


class SessionContextStore:
    def __init__(self, redis_client=None, ttl_seconds: int = 6 * 3600, max_items: int = 12):
        self.r = redis_client
        self.ttl = ttl_seconds
        self.max_items = max_items
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
    def _key(skey: str) -> str:
        return f"sa:ctx:{skey}"

    # ---- storage
    def _load(self, skey: str) -> list[dict]:
        if self.r is not None:
            try:
                raw = self.r.get(self._key(skey))
                return json.loads(raw) if raw else []
            except Exception:  # noqa: BLE001
                log.exception("session context read failed")
                return []
        with self._lock:
            exp, items = self._mem.get(skey, (0, []))
            return list(items) if exp > time.time() else []

    def _save(self, skey: str, items: list[dict]) -> None:
        items = items[: self.max_items]
        if self.r is not None:
            try:
                self.r.set(self._key(skey), json.dumps(items), ex=self.ttl)
                return
            except Exception:  # noqa: BLE001
                log.exception("session context write failed")
        with self._lock:
            self._mem[skey] = (time.time() + self.ttl, items)

    # ---- the log
    def remember(self, skey: str | None, kind: str, data: dict) -> None:
        """Add one entry to this session's window, newest first. No session (not verified): nothing kept."""
        if not skey:
            return
        item = {"kind": kind, **{k: v for k, v in data.items() if v is not None}, "seen_at": time.time()}
        self._save(skey, [item] + self._load(skey))

    def remember_payment(self, skey: str | None, evidence: dict) -> None:
        """A receipt / UTR shared in this session, masked (the full evidence lives in the claims ledger)."""
        utr = evidence.get("utr")
        if not utr:
            return
        self.remember(skey, "payment", {"utr_last4": utr[-4:], "amount": evidence.get("amount"),
                                        "date": evidence.get("txn_date"), "source": evidence.get("source")})

    def window(self, skey: str | None, limit: int = 8) -> list[dict]:
        """What the model and reviewers see: this session's entries, newest first."""
        if not skey:
            return []
        out = []
        for it in self._load(skey):
            e = {k: v for k, v in it.items() if k != "seen_at" and v not in (None, "")}
            e["at"] = it.get("seen_at")
            out.append(e)
            if len(out) >= limit:
                break
        return out

    # ---- small per-key state with its own lifetime (e.g. the hardship ladder per loan, 7 days)
    def get_state(self, key: str) -> dict:
        items = self._load(f"state:{key}")
        return items[0] if items else {}

    def set_state(self, key: str, data: dict, ttl_seconds: int) -> None:
        old, self.ttl = self.ttl, ttl_seconds
        try:
            self._save(f"state:{key}", [data])
        finally:
            self.ttl = old

    def forget(self, skey: str | None) -> None:
        if not skey:
            return
        if self.r is not None:
            try:
                self.r.delete(self._key(skey))
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._mem.pop(skey, None)
