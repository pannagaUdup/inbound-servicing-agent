"""D7 payment-claim verifier (owner: T3). The ledger, not the screenshot, decides.

Tamper forensics is parked: a claim is accepted only on an exact D7 match, so
every edited or fake screenshot ends in "not accepted + flagged".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

from servicing_agent.contracts.tools import PaymentFeed
from servicing_agent.data.generators.core import OUR_VPA


class Verdict(str, Enum):
    MATCHED = "MATCHED"                      # mark paid, issue receipt
    PENDING = "PENDING"                      # not in D7 yet, inside settlement window
    MISMATCH = "MISMATCH"                    # UTR exists but amount/date/status/payee differ
    OTHER_ACCOUNT = "OTHER_ACCOUNT"          # UTR belongs to another loan
    REPLAY = "REPLAY"                        # UTR already claimed
    NOT_FOUND = "NOT_FOUND"                  # outside window, or impossible values
    NEED_UTR = "NEED_UTR"                    # nothing to reconcile against


# Verdicts that are "caught": never accepted, ticket to fraud review.
FLAGGED = {Verdict.MISMATCH, Verdict.OTHER_ACCOUNT, Verdict.REPLAY}


@dataclass
class ClaimResult:
    verdict: Verdict
    reasons: list[str]
    txn: dict | None = None

    @property
    def accepted(self) -> bool:
        return self.verdict == Verdict.MATCHED


def _edit_distance_le1(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) != len(b):
        return False
    return sum(x != y for x, y in zip(a, b)) <= 1


def reconcile(
    feed: PaymentFeed,
    account_id: str,
    utr: str | None,
    amount: float | None,
    txn_date: str | None,
    payee_vpa: str | None = None,
    window_hours: int = 24,
    now: datetime | None = None,
) -> ClaimResult:
    now = now or datetime.utcnow()
    if not utr and not (amount and txn_date):
        return ClaimResult(Verdict.NEED_UTR, ["no UTR and no amount+date"])

    # Impossible values are caught before any lookup.
    reasons: list[str] = []
    claimed_day: date | None = None
    if txn_date:
        try:
            claimed_day = date.fromisoformat(txn_date[:10])
            if claimed_day > now.date():
                return ClaimResult(Verdict.NOT_FOUND, ["future transaction date"])
        except ValueError:
            reasons.append("unparseable date")
    if utr and (len(utr) != 12 or not utr.isdigit()):
        return ClaimResult(Verdict.NOT_FOUND, ["malformed UTR"])
    if payee_vpa and payee_vpa.lower() != OUR_VPA:
        return ClaimResult(Verdict.MISMATCH, ["payee is not ours"])

    txn = feed.find_by_utr(utr) if utr else None

    # OCR tolerance: a single candidate within edit distance 1 counts as the same UTR.
    if txn is None and amount and txn_date:
        candidates = feed.find_candidates(account_id, amount, txn_date, days=1)
        close = [c for c in candidates if not utr or _edit_distance_le1(c["utr"], utr)]
        if len(close) == 1:
            txn = close[0]
            reasons.append("matched via amount/date (OCR tolerance)")

    if txn is None:
        if claimed_day and now - datetime.combine(claimed_day, datetime.min.time()) <= timedelta(hours=window_hours):
            return ClaimResult(Verdict.PENDING, reasons + ["not in D7 yet, inside settlement window"])
        return ClaimResult(Verdict.NOT_FOUND, reasons + ["not in D7"])

    if txn["account_id"] != account_id:
        return ClaimResult(Verdict.OTHER_ACCOUNT, ["UTR mapped to a different loan or suspense"], None)
    if feed.is_claimed(txn["utr"]):
        return ClaimResult(Verdict.REPLAY, ["UTR already claimed"], None)
    if amount is not None and abs(float(txn["amount"]) - float(amount)) > 0.5:
        return ClaimResult(Verdict.MISMATCH, ["amount differs from ledger"], None)
    if claimed_day and abs((date.fromisoformat(txn["txn_date"]) - claimed_day).days) > 1:
        return ClaimResult(Verdict.MISMATCH, ["date differs from ledger"], None)
    if txn["status"] != "success":
        return ClaimResult(Verdict.MISMATCH, [f"ledger status is {txn['status']}"], None)
    return ClaimResult(Verdict.MATCHED, reasons, txn)
