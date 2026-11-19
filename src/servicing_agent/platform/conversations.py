"""Conversation session store (owner: T2). In-memory for the MVP.

TODO(T2): move to Redis (session + per-conversation lock) and Postgres (transcript).
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any


class ConversationStore:
    def __init__(self):
        self._data: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    def lock(self, conversation_id: str) -> threading.Lock:
        """Guarantees in-order processing per borrower."""
        return self._locks[conversation_id]

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
