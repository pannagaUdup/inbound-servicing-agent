"""Conversation session store (owner: T2).

Shared, production-grade session state:

* **Redis** (`sa:convstate:{cid}`) is the source of truth — shared across processes/workers, so
  a verified scope or pending YES/NO set by one worker is visible to the next, whichever process
  handles the borrower's following message.
* An **in-process cache** holds the live dict only for the duration of a turn (so repeated
  ``load()`` calls within one turn accumulate mutations), then is evicted on ``persist()`` so the
  next turn re-reads the shared Redis copy.
* **Postgres** (`conversation_state`) is the durable archive: written through on persist and read
  as a fallback on a Redis miss (survives a Redis flush; keeps history).

Every Redis/DB access is guarded — a backing-store failure must never fail a turn ("degrade,
don't crash"); it falls back to the in-process dict. The per-conversation lock stays in-process:
the inbound queue (platform/queue.py) already serializes a conversation across workers via its own
Redis lock, so this lock only needs to guard the direct (simulate) path and re-entrancy.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_KEY = "sa:convstate:"
_TTL = 7 * 24 * 3600  # 7 days in Redis; Postgres is the long-term archive


def _default(conversation_id: str) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "session": {"verification": {}, "scope": None, "pending_tasks": [], "pending_change": None,
                    "clarify_count": 0, "lang": "en"},
        "transcript": [], "events": [], "tickets": [],
    }


def _normalize(data: dict, conversation_id: str) -> dict:
    data.setdefault("conversation_id", conversation_id)
    data.setdefault("session", _default(conversation_id)["session"])
    for k in ("transcript", "events", "tickets"):
        data.setdefault(k, [])
    return data


class ConversationStore:
    def __init__(self, redis_url: str | None = None, session_factory=None):
        self._data: dict[str, dict[str, Any]] = {}     # transient per-turn working cache
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._Session = session_factory
        self._r = None
        if redis_url:
            try:
                import redis
                self._r = redis.from_url(redis_url, decode_responses=True)
                self._r.ping()
            except Exception:  # noqa: BLE001 - Redis optional; fall back to in-process + DB
                logger.warning("ConversationStore: Redis unavailable (%s); using in-process + DB", redis_url)
                self._r = None

    def lock(self, conversation_id: str) -> threading.RLock:
        """One re-entrant lock per borrower (in-process). Cross-process ordering on the live path is
        handled by the inbound queue's own per-conversation Redis lock."""
        with self._locks_guard:
            lk = self._locks.get(conversation_id)
            if lk is None:
                lk = self._locks[conversation_id] = threading.RLock()
            return lk

    # ---- backing-store helpers (all guarded)
    def _redis_get(self, cid: str) -> dict | None:
        if self._r is None:
            return None
        try:
            raw = self._r.get(_KEY + cid)
            return json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            logger.exception("convstate redis get failed for %s", cid)
            return None

    def _redis_put(self, cid: str, data: dict) -> None:
        if self._r is None:
            return
        try:
            self._r.set(_KEY + cid, json.dumps(data), ex=_TTL)
        except Exception:  # noqa: BLE001
            logger.exception("convstate redis put failed for %s", cid)

    def _db_get(self, cid: str) -> dict | None:
        if self._Session is None:
            return None
        try:
            from servicing_agent.platform.db import ConversationState
            with self._Session() as s:
                row = s.get(ConversationState, cid)
            return row.data if row and isinstance(row.data, dict) else None
        except Exception:  # noqa: BLE001
            logger.exception("convstate db get failed for %s", cid)
            return None

    def _db_put(self, cid: str, data: dict) -> None:
        if self._Session is None:
            return
        try:
            from servicing_agent.platform.db import ConversationState
            with self._Session.begin() as s:
                s.merge(ConversationState(conversation_id=cid, data=data, updated_at=datetime.utcnow()))
        except Exception:  # noqa: BLE001
            logger.exception("convstate db put failed for %s", cid)

    # ---- API
    def load(self, conversation_id: str) -> dict[str, Any]:
        """Return the live conversation dict. Within a turn (cid already checked out) returns the
        working copy; otherwise reads the shared Redis copy (then DB, then a fresh default)."""
        cached = self._data.get(conversation_id)
        if cached is not None:
            return cached
        data = self._redis_get(conversation_id) or self._db_get(conversation_id) or _default(conversation_id)
        data = _normalize(data, conversation_id)
        self._data[conversation_id] = data
        return data

    def persist(self, conversation_id: str) -> None:
        """Write the turn's result through to Redis (shared) + Postgres (durable), then evict the
        working copy so the next turn re-reads the shared state."""
        data = self._data.get(conversation_id)
        if data is None:
            return
        self._redis_put(conversation_id, data)
        self._db_put(conversation_id, data)
        self._data.pop(conversation_id, None)

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        return self._data.get(conversation_id) or self._redis_get(conversation_id) or self._db_get(conversation_id)

    def all(self, limit: int = 500) -> list[dict[str, Any]]:
        """Every conversation for the Conversations screen: the Postgres archive (durable, survives a
        Redis flush/restart) merged with the live Redis copies and any in-flight local turns, with the
        freshest taking precedence."""
        out: dict[str, dict] = {}
        # 1) durable archive
        if self._Session is not None:
            try:
                from sqlalchemy import select
                from servicing_agent.platform.db import ConversationState
                with self._Session() as s:
                    rows = s.execute(select(ConversationState).order_by(ConversationState.updated_at.desc())
                                     .limit(limit)).scalars().all()
                for row in rows:
                    if isinstance(row.data, dict):
                        out[row.conversation_id] = row.data
            except Exception:  # noqa: BLE001
                logger.exception("convstate db scan failed")
        # 2) live Redis copies (override archive)
        if self._r is not None:
            try:
                for key in self._r.scan_iter(_KEY + "*", count=200):
                    raw = self._r.get(key)
                    if raw:
                        d = json.loads(raw)
                        out[d.get("conversation_id") or key] = d
            except Exception:  # noqa: BLE001
                logger.exception("convstate redis scan failed")
        out.update(self._data)  # in-flight turns not yet persisted
        return list(out.values())
