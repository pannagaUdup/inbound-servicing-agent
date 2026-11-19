"""Receipt authenticity score for payment claims that did not match the ledger (D7).

A small logistic model over features that compare the claim with the ledger and the loan:
does the UTR exist, on this loan, with this amount and date; is the payee ours; does the amount
look like this borrower's EMI / overdue; how confident was the extraction. It returns a
"supporting score" in [0, 1]: how strongly the evidence supports the payment being genuine
but not yet reflected.

* ``supported``   (>= 0.6) the claim is plausible, e.g. a fresh UTR that isn't in D7 yet.
* ``unsupported`` (< 0.6)  something contradicts the ledger.
* ``likely_fake`` (<= 0.2) strong contradiction: an edited amount on a known UTR, someone
  else's UTR, a replayed UTR, a future date. Routed to fraud review, never said to the borrower.

The score never accepts a payment. Only an exact ledger match does. The weights are set by hand
and readable; they can be refit on the labelled eval scenarios (``is_fake``) without changing callers.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date

from servicing_agent.agent.tools.reconciliation import ClaimResult, Verdict, payee_matches

WEIGHTS: dict[str, float] = {
    "bias": -0.5,
    "utr_in_ledger_same_loan": 1.5,
    "amount_matches_ledger": 1.5,
    "amount_differs_from_ledger": -4.5,     # edited amount on a real UTR: the strongest sign of an edit
    "date_matches_ledger": 0.5,
    "date_differs_from_ledger": -1.5,
    "utr_on_other_loan": -3.0,
    "utr_already_claimed_elsewhere": -3.0,
    "payee_is_ours": 0.5,
    "payee_not_ours": -2.5,
    "receipt_says_success": 0.3,
    "receipt_not_success": -1.0,
    "future_date": -4.0,
    "malformed_utr": -2.0,
    "amount_fits_loan": 1.0,                # equals the EMI, overdue, or a whole number of EMIs
    "extraction_confidence": 1.0,           # (confidence - 0.5)
    "low_confidence_fields": -0.3,          # per field
    "has_time": 0.2,
}
SUPPORTED, LIKELY_FAKE = 0.6, 0.2


@dataclass
class Authenticity:
    score: float
    label: str                                # supported | unsupported | likely_fake
    signals: dict[str, float] = field(default_factory=dict)   # feature -> contribution

    def as_dict(self) -> dict:
        d = asdict(self)
        d["score"] = round(self.score, 3)
        d["signals"] = {k: round(v, 2) for k, v in self.signals.items() if v}
        return d


def _amount_fits(amount: float | None, loan: dict) -> bool:
    if not amount:
        return False
    emi = float(loan.get("emi_amount") or 0)
    overdue = float(loan.get("overdue_amount") or 0)
    if any(abs(amount - x) <= 0.5 for x in (emi, overdue) if x):
        return True
    return emi > 0 and abs(amount / emi - round(amount / emi)) < 0.01 and 1 <= round(amount / emi) <= 12


def score_claim(slots: dict, result: ClaimResult, ledger_txn: dict | None, account_id: str, loan: dict,
                *, confidence: float = 1.0, low_confidence_fields: int = 0, today: date | None = None) -> Authenticity:
    utr, amount, txn_date = slots.get("utr"), slots.get("amount"), slots.get("txn_date")
    f: dict[str, float] = {"bias": 1.0}
    if utr and (len(utr) != 12 or not utr.isdigit()):
        f["malformed_utr"] = 1
    if ledger_txn:
        if ledger_txn["account_id"] == account_id:
            f["utr_in_ledger_same_loan"] = 1
            if amount is not None:
                key = "amount_matches_ledger" if abs(float(ledger_txn["amount"]) - float(amount)) <= 0.5 \
                    else "amount_differs_from_ledger"
                f[key] = 1
            if txn_date:
                try:
                    gap = abs((date.fromisoformat(ledger_txn["txn_date"]) - date.fromisoformat(txn_date[:10])).days)
                    f["date_matches_ledger" if gap <= 1 else "date_differs_from_ledger"] = 1
                except ValueError:
                    pass
        else:
            f["utr_on_other_loan"] = 1
    if result.verdict == Verdict.OTHER_ACCOUNT:
        f["utr_on_other_loan"] = 1
    if result.verdict == Verdict.REPLAY:
        f["utr_already_claimed_elsewhere"] = 1
    if "future transaction date" in result.reasons:
        f["future_date"] = 1
    if slots.get("payee_vpa"):
        f["payee_is_ours" if payee_matches(slots["payee_vpa"]) else "payee_not_ours"] = 1
    status = (slots.get("status") or "").lower()
    if status:
        f["receipt_says_success" if status == "success" else "receipt_not_success"] = 1
    if _amount_fits(amount, loan) and "amount_differs_from_ledger" not in f:
        f["amount_fits_loan"] = 1
    f["extraction_confidence"] = max(0.0, min(1.0, confidence)) - 0.5
    f["low_confidence_fields"] = float(low_confidence_fields)
    if slots.get("txn_time"):
        f["has_time"] = 1

    contributions = {k: WEIGHTS[k] * v for k, v in f.items()}
    z = sum(contributions.values())
    score = 1 / (1 + math.exp(-z))
    label = "supported" if score >= SUPPORTED else "likely_fake" if score <= LIKELY_FAKE else "unsupported"
    return Authenticity(score=score, label=label, signals=contributions)
