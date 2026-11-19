"""Conversation session store (owner: T2).

In-memory for speed, with optional Postgres persistence so a restart does not drop verified
scopes and pending YES/NO actions (and the Conversations view survives too). The DB is a
write-through cache of the live dict; every DB access is guarded — persistence must never fail
a turn ("degrade, don't crash").
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def _default(conversation_id: str) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "session": {"verification": {}, "scope": None, "pending_tasks": [], "pending_change": None,
                    "clarify_count": 0, "lang": "en"},
        "transcript": [], "events": [], "tickets": [],
    }


class ConversationStore:
    def __init__(self, session_factory=None):
        self._data: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._Session = session_factory  # optional; when set, load rehydrates and persist writes through

    def lock(self, conversation_id: str) -> threading.RLock:
        """One lock per borrower: their messages are handled one at a time (re-entrant)."""
        with self._locks_guard:
            lk = self._locks.get(conversation_id)
            if lk is None:
                lk = self._locks[conversation_id] = threading.RLock()
            return lk

    def load(self, conversation_id: str) -> dict[str, Any]:
        if conversation_id in self._data:
            return self._data[conversation_id]
        if self._Session is not None:
            try:
                from servicing_agent.platform.db import ConversationState
                with self._Session() as s:
                    row = s.get(ConversationState, conversation_id)
                if row and isinstance(row.data, dict):
                    data = row.data
                    data.setdefault("conversation_id", conversation_id)
                    data.setdefault("session", _default(conversation_id)["session"])
                    for k in ("transcript", "events", "tickets"):
                        data.setdefault(k, [])
                    self._data[conversation_id] = data
                    return data
            except Exception:  # noqa: BLE001 - rehydrate is best-effort
                logger.exception("conversation rehydrate failed for %s", conversation_id)
        return self._data.setdefault(conversation_id, _default(conversation_id))

    def persist(self, conversation_id: str) -> None:
        """Write the live conversation through to the DB. No-op without a session_factory."""
        if self._Session is None:
            return
        data = self._data.get(conversation_id)
        if data is None:
            return
        try:
            from servicing_agent.platform.db import ConversationState
            with self._Session.begin() as s:
                s.merge(ConversationState(conversation_id=conversation_id, data=data,
                                          updated_at=datetime.utcnow()))
        except Exception:  # noqa: BLE001 - never fail a turn on a persistence write
            logger.exception("conversation persist failed for %s", conversation_id)

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        return self._data.get(conversation_id)

    def all(self) -> list[dict[str, Any]]:
        return list(self._data.values())
