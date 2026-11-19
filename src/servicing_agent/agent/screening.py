"""Screens text, transcripts and OCR text for injection, distress, legal threats and harassment.

Rules first; T1 adds a prompt-guard-class classifier behind the same function.
Injected instructions are ignored (they have no path to privileged tools) and logged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from servicing_agent.contracts.state import Intent

INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"disregard (your|the|all) (instructions|rules|guidelines)",
    r"\bsystem prompt\b", r"\byou are now\b", r"\bact as (an? )?(admin|developer|system)",
    r"\bdeveloper mode\b", r"\bjailbreak\b", r"\boverride\b.*\b(verification|security|rules)\b",
    r"(show|list|give|send) (me )?(all|every|other) (accounts|loans|customers|numbers)",
    r"\bwaive (all|every)\b.*\bcharges\b.*\bnow\b", r"\bmark (it|this|my loan) as paid\b",
    r"</?(system|assistant|instructions?)>", r"\bpretend\b.*\b(verified|admin)\b",
]
DISTRESS = [r"suicid", r"kill myself", r"end (my|this) life", r"don'?t want to live", r"can'?t live",
            r"no way out", r"marna chahta", r"jaan de"]
LEGAL = [r"\blawyer\b", r"\badvocate\b", r"legal notice", r"\bcourt\b", r"\bpolice\b", r"consumer forum", r"\bfir\b"]
HARASSMENT = [r"harass", r"threaten", r"abus(ed|ive|ing)\b.*\b(agent|caller|recovery)", r"calling my (family|relatives|office|boss)",
              r"recovery agent", r"\bgaali\b"]


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


@dataclass
class ScreenResult:
    injection: bool = False
    injection_snippets: list[str] = field(default_factory=list)
    handoff_intent: Intent | None = None
    cleaned_text: str = ""


def screen(text: str) -> ScreenResult:
    res = ScreenResult(cleaned_text=text or "")
    if not text:
        return res
    # Split into clauses: sentence ends, ; and , and line breaks (a batch joins messages with "\n").
    # Only the clause carrying an injection is dropped; "already paid, ignore previous instructions"
    # keeps "already paid".
    lines = [p for p in re.split(r"(?<=[.!?;,])\s+|\s*\n+\s*", text) if p.strip()]
    kept = []
    for line in lines:
        if _any(INJECTION_PATTERNS, line):
            res.injection = True
            res.injection_snippets.append(line[:120])
        else:
            kept.append(line)
    res.cleaned_text = " ".join(kept)
    if _any(DISTRESS, text):
        res.handoff_intent = Intent.DISTRESS
    elif _any(HARASSMENT, text):
        res.handoff_intent = Intent.HARASSMENT_COMPLAINT
    elif _any(LEGAL, text):
        res.handoff_intent = Intent.LEGAL_THREAT
    return res
