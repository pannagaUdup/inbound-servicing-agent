"""Idempotent outbound sends: each reply is sent at most once.

Every reply has a send key: the conversation, the turn that produced it (the inbound message id, or the
recheck job) and its position in the turn. Before sending, the key is claimed (Redis ``SET NX``, 24 h). A key
that is already claimed is skipped, so a WhatsApp webhook redelivery, a queue recovery after a restart, or a
duplicate recheck never sends the same reply twice. If the earlier attempt failed for certain, its key is
released and the duplicate run sends it.

Retries follow the same rule: retry only when the message was certainly *not* delivered (connection refused,
HTTP 429 / 5xx). A timeout after the request was sent may have been delivered, so it is not retried (that
would risk a double message); it is logged as ``unknown``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time

log = logging.getLogger(__name__)

TTL_SECONDS = 24 * 3600


def send_key(conversation_id: str, turn_id: str, index: int, text: str | None = None) -> str:
    raw = f"{conversation_id}|{turn_id}|{index}|{text or ''}"
    return "sa:sent:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def classify_failure(exc: Exception) -> str:
    """'retry' (certainly not delivered, may succeed later), 'failed' (rejected), 'unknown' (may have
    been delivered: don't resend)."""
    name = type(exc).__name__
    if name in ("ConnectError", "ConnectTimeout", "PoolTimeout"):
        return "retry"
    if "Timeout" in name or name in ("RemoteProtocolError", "ReadError"):
        return "unknown"
    m = re.search(r"WhatsApp API (\d{3})", str(exc))
    if m:
        code = int(m.group(1))
        return "retry" if code == 429 or code >= 500 else "failed"
    return "failed"


class SendLedger:
    def __init__(self, redis_client=None, ttl_seconds: int = TTL_SECONDS):
        self.r = redis_client
        self.ttl = ttl_seconds
        self._mem: dict[str, float] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_url(cls, redis_url: str | None) -> "SendLedger":
        client = None
        if redis_url:
            try:
                import redis
                client = redis.from_url(redis_url, decode_responses=True)
                client.ping()
            except Exception as exc:  # noqa: BLE001
                log.warning("send ledger: Redis unavailable (%s); in-memory only", exc)
                client = None
        return cls(client)

    def claim(self, key: str) -> bool:
        """True if this send is ours to do; False if it was already sent (or is being sent)."""
        if self.r is not None:
            try:
                return bool(self.r.set(key, "1", nx=True, ex=self.ttl))
            except Exception:  # noqa: BLE001
                log.exception("send ledger claim failed; sending anyway")
                return True
        now = time.time()
        with self._lock:
            if self._mem.get(key, 0) > now:
                return False
            self._mem[key] = now + self.ttl
            return True

    def release(self, key: str) -> None:
        """The send certainly failed: let a later run send it."""
        if self.r is not None:
            try:
                self.r.delete(key)
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._mem.pop(key, None)
