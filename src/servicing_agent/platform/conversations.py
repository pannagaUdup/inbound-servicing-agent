"""Conversation session store (owner: T2). In-memory for the MVP.

TODO(T2): move to Redis (session + per-conversation lock) and Postgres (transcript).
"""

from __future__ import annotations

import threading
from typing import Any


class ConversationStore:
    def __init__(self):
        self._data: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def lock(self, conversation_id: str) -> threading.RLock:
        """One lock per borrower: their messages are handled one at a time.

        Re-entrant, so the API can hold it across perception *and* the agent turn
        (``run_turn`` takes it again inside). Creation is guarded so two messages arriving
        together can't each make their own lock. Ordering is "first to the lock", which
        matches arrival order closely but isn't strict FIFO; the Redis stream worker (T2)
        gives strict per-conversation ordering.
        """
        with self._locks_guard:
            lk = self._locks.get(conversation_id)
            if lk is None:
                lk = self._locks[conversation_id] = threading.RLock()
            return lk

    def load(self, conversation_id: str) -> dict[str, Any]:
        return self._data.setdefault(conversation_id, {
            "conversation_id": conversation_id,
            "session": {"verification": {}, "scope": None, "pending_tasks": [], "pending_change": None,
                        "clarify_count": 0, "lang": "en"},
            "transcript": [], "events": [], "tickets": [],
        })

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        return self._data.get(conversation_id)

    def all(self) -> list[dict[str, Any]]:
        return list(self._data.values())
