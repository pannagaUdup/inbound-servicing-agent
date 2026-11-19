"""Identifier formats shared by generators, verification and the output guard."""

from __future__ import annotations

import re

ACCOUNT_RE = re.compile(r"\bCN\d{10}\b", re.IGNORECASE)
UTR_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
OTP_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")


def luhn_check_digit(digits: str) -> int:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def make_account_number(nine_digits: str) -> str:
    """CN + 9 digits + Luhn check digit."""
    assert len(nine_digits) == 9 and nine_digits.isdigit()
    return f"CN{nine_digits}{luhn_check_digit(nine_digits)}"


def is_valid_account_number(value: str) -> bool:
    value = value.strip().upper()
    if not re.fullmatch(r"CN\d{10}", value):
        return False
    body, check = value[2:11], int(value[11])
    return luhn_check_digit(body) == check


def extract_account_number(text: str | None) -> str | None:
    if not text:
        return None
    m = ACCOUNT_RE.search(text)
    return m.group(0).upper() if m else None


def normalize_mobile(value: str) -> str:
    """Return the last 10 digits of an Indian mobile number (WhatsApp senders arrive as 91XXXXXXXXXX)."""
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:]
