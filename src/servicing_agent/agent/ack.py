"""Acknowledgement classifier for outbound campaigns (owner: T1).

Phase 1: the real decision ("did the borrower acknowledge the campaign message?")
is an LLM job. This is the seam — a ``MockAckClassifier`` with a keyword rule so the
journey works end to end now; swap in an LLM-backed classifier behind the same
``AckClassifier`` protocol later without touching the campaign code.
"""

from __future__ import annotations

import re
from typing import Protocol

_YES = re.compile(
    r"\b(yes|yeah|yep|yup|ok|okay|sure|done|paid|pay|haan|ha|ji|theek|thik|ok ji|👍|✅|हाँ| हां)\b",
    re.IGNORECASE,
)
_NO = re.compile(r"\b(no|nope|not|nahi|nahin|stop|later|busy|नहीं)\b", re.IGNORECASE)


class AckClassifier(Protocol):
    def is_ack(self, text: str | None) -> bool: ...


class MockAckClassifier:
    """Deterministic stand-in for the LLM ack classifier.

    Returns True when the reply reads as an affirmative acknowledgement. A clear
    negative wins over an affirmative token in the same message.
    """

    def is_ack(self, text: str | None) -> bool:
        if not text:
            return False
        t = text.strip()
        if _NO.search(t):
            return False
        return bool(_YES.search(t))


def make_ack_classifier(kind: str = "mock") -> AckClassifier:
    # TODO(T1): kind == "llm" -> Claude-backed classifier behind this same protocol.
    return MockAckClassifier()
