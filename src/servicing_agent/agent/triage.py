"""Per-turn triage: is this compliance-sensitive, and how is the borrower feeling?

Deterministic rules, run after planning. The result is:

* written to the audit log (``triage`` event) and the normalized view,
* attached to every ticket and case raised this turn, so a supervisor sees priority and mood,
* used to adapt the reply (hardship gets an acknowledgement and priority).

It never decides an outcome. Waivers, settlements, reversals and disputes always go to people.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from servicing_agent.contracts.state import Intent, Task

# Requests a person must decide, under the lender's compliance rules.
APPROVAL_INTENTS = {Intent.WAIVER_REQUEST, Intent.SETTLEMENT_REQUEST}
COMPLIANCE_INTENTS = APPROVAL_INTENTS | {
    Intent.CHARGE_REVERSAL, Intent.DISPUTE, Intent.COMPLAINT,
    Intent.LEGAL_THREAT, Intent.HARASSMENT_COMPLAINT, Intent.DISTRESS, Intent.ESCALATION_REQUEST,
}

HARDSHIP = [r"lost (my )?job", r"job loss", r"unemploy", r"no (job|income|salary|work)", r"salary (not|hasn'?t) (come|credited)",
            r"hospital", r"medical", r"surgery", r"illness", r"\bsick\b", r"passed away", r"\bdeath\b", r"accident",
            r"business (loss|down|closed)", r"shop closed", r"can'?t (afford|pay)", r"cannot (afford|pay)", r"unable to pay", r"(won'?t|will not|not) be able to (pay|make)",
            r"not able to pay", r"(don'?t|do not) have (enough )?(money|funds)",
            r"no money", r"struggling", r"financial (problem|difficult|crisis|trouble)", r"naukri (chali|gayi)",
            r"paise nahi"]
FRUSTRATED = [r"\bworst\b", r"fed up", r"\bangry\b", r"\buseless\b", r"\bpathetic\b", r"\bridiculous\b",
              r"again and again", r"how many times", r"not happy", r"\bharass", r"\bcheat", r"\bfraud\b", r"!!+",
              r"\bstop calling\b", r"\bdisgusting\b", r"\bterrible\b",
              r"too (much|many) (of )?(problems?|trouble|issues?)", r"\bfrustrat"]
POSITIVE = [r"\bthank(s| you)\b", r"\bgreat\b", r"\bperfect\b", r"\bhelpful\b", r"\bdhanyavaad\b", r"\bshukriya\b"]


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


@dataclass
class Triage:
    category: str = "routine"                 # "compliance" | "routine"
    sentiment: str = "neutral"                # "distressed" | "frustrated" | "neutral" | "positive"
    hardship: bool = False
    needs_approval: bool = False              # waiver / settlement: supervisor -> compliance
    priority: str = "normal"                  # "high" | "normal"
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def triage(text: str, tasks: list[Task], handoff_intent: Intent | None = None) -> Triage:
    text = text or ""
    intents = {t.intent for t in tasks}
    if handoff_intent:
        intents.add(handoff_intent)
    t = Triage()
    flagged = sorted(i.value for i in intents & COMPLIANCE_INTENTS)
    if flagged:
        t.category = "compliance"
        t.reasons += [f"intent:{i}" for i in flagged]
    t.needs_approval = bool(intents & APPROVAL_INTENTS)
    t.hardship = _any(HARDSHIP, text)
    if handoff_intent == Intent.DISTRESS or t.hardship:
        t.sentiment = "distressed"
    elif _any(FRUSTRATED, text) or intents & {Intent.COMPLAINT, Intent.HARASSMENT_COMPLAINT}:
        t.sentiment = "frustrated"
    elif _any(POSITIVE, text):
        t.sentiment = "positive"
    if t.hardship:
        t.reasons.append("hardship")
    if t.sentiment in ("distressed", "frustrated") and t.category == "compliance":
        t.priority = "high"
    return t
