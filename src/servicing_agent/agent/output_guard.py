"""Deterministic check on every outgoing message. Blocks and replaces; never edits in place.

Checks:
1. Disclosure: no account number other than the verified one; no mobile numbers;
   before verification, no amounts, dates, UTRs or account numbers at all.
2. Grounding: every amount, date and long number must come from this turn's tool
   results (recorded by the gateway) or be a known constant.
3. Commitment: no promise about waivers, settlements, charges or approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from servicing_agent.data.identifiers import ACCOUNT_RE

URL_RE = re.compile(r"https?://\S+")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
MOBILE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
NUMBER_RE = re.compile(r"(?<![\w-])\d[\d,]*(?:\.\d+)?")
COMMITMENT_RE = re.compile(
    r"\b(will|shall|can|we'?ll) (be )?(waive|waived|remove|removed|reverse|reversed|settle|approve|approved)\b"
    r"|\b(has|have) been (waived|approved|reversed|settled)\b"
    r"|\bguarantee"
    r"|\bsettle (it |this )?for\b"
    r"|माफ़? कर दि",
    re.IGNORECASE,
)


@dataclass
class GuardResult:
    ok: bool
    reason: str | None = None
    detail: str | None = None


def check(text: str, *, verified_account: str | None, allowed_numbers: set[float],
          allowed_strings: set[str], constants: set[str] = frozenset()) -> GuardResult:
    if COMMITMENT_RE.search(text):
        return GuardResult(False, "commitment", COMMITMENT_RE.search(text).group(0))

    for acc in ACCOUNT_RE.findall(text):
        if verified_account is None or acc.upper() != verified_account:
            return GuardResult(False, "disclosure", "account number")

    scrubbed = URL_RE.sub(" ", text)
    for s in sorted(set(allowed_strings) | set(constants), key=len, reverse=True):
        if s and len(s) >= 3:
            scrubbed = scrubbed.replace(s, " ")
    if verified_account:
        scrubbed = scrubbed.replace(verified_account, " ")

    if MOBILE_RE.search(scrubbed):
        return GuardResult(False, "disclosure", "mobile number")
    if DATE_RE.search(scrubbed):
        return GuardResult(False, "disclosure" if verified_account is None else "ungrounded", "date")

    for raw in NUMBER_RE.findall(scrubbed):
        value = float(raw.replace(",", "")) if raw.replace(",", "").replace(".", "", 1).isdigit() else None
        if value is None:
            continue
        if value < 100 and "." not in raw and "," not in raw:
            continue  # small counts like "6-digit", "3 days"
        if verified_account is None:
            return GuardResult(False, "disclosure", raw)
        if round(value, 2) not in allowed_numbers:
            return GuardResult(False, "ungrounded", raw)
    return GuardResult(True)
