"""Policy clauses: retrieval over the servicing policy document (config/policy/servicing_policy.md).

The approval stages, consequences and escalation rules live in a versioned policy document, not in
templates or code. The agent retrieves the clauses that apply to a situation and:

* quotes each clause's **borrower** line word for word (compliance-approved wording, so no model
  rewrites it and the output guard still applies);
* records the clause IDs on the turn, the ticket and the approval context pack, so reviewers see
  exactly which policy the agent applied.

Retrieval is BM25 over title + tags + text, with optional required tags as a hard filter. It is
deterministic and needs no model or network. An embedding retriever can replace ``_score`` later
without changing callers.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parents[3] / "config" / "policy" / "servicing_policy.md"
_HEADING = re.compile(r"^##\s+([A-Z]{2,4}-\d{2})\s+·\s+(.+?)\s*$")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "is", "it", "in", "on", "for", "be", "my", "i", "if", "what",
         "how", "does", "do", "will", "can", "this", "that", "with", "as", "are", "by", "not", "no", "me"}


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


@dataclass(frozen=True)
class Clause:
    id: str
    title: str
    tags: tuple[str, ...]
    borrower: str | None
    rule: str
    order: int

    def ref(self) -> dict:
        return {"id": self.id, "title": self.title, "rule": self.rule, "borrower": self.borrower}


@dataclass
class PolicyStore:
    clauses: list[Clause]
    version: str = ""
    _tf: list[Counter] = field(default_factory=list, repr=False)
    _df: Counter = field(default_factory=Counter, repr=False)
    _avg: float = 0.0

    def __post_init__(self):
        self._tf = [Counter(_tokens(f"{c.title} {' '.join(c.tags)} {' '.join(c.tags)} {c.rule}")) for c in self.clauses]
        for tf in self._tf:
            self._df.update(set(tf))
        self._avg = sum(sum(tf.values()) for tf in self._tf) / max(1, len(self._tf))

    @classmethod
    def load(cls, path: Path = POLICY_PATH) -> "PolicyStore":
        text = path.read_text(encoding="utf-8")
        version = (re.search(r"^Version:\s*([^.]+)", text, re.M) or [None, ""])[1].strip()
        clauses, cur, order = [], None, 0
        for line in text.splitlines() + ["## END-00 · end"]:
            m = _HEADING.match(line)
            if m:
                if cur:
                    clauses.append(Clause(cur["id"], cur["title"], tuple(cur["tags"]), cur["borrower"],
                                          " ".join(cur["rule"]).strip(), order))
                    order += 1
                cur = {"id": m.group(1), "title": m.group(2), "tags": [], "borrower": None, "rule": []}
            elif cur is not None:
                if line.startswith("tags:"):
                    cur["tags"] = [t.strip().lower() for t in line[5:].split(",") if t.strip()]
                elif line.startswith("borrower:"):
                    cur["borrower"] = line[9:].strip()
                elif line.strip():
                    cur["rule"].append(line.strip())
        return cls([c for c in clauses if c.id != "END-00"], version)

    def get(self, clause_id: str) -> Clause:
        return next(c for c in self.clauses if c.id == clause_id)

    def _score(self, q: list[str], i: int, k1: float = 1.4, b: float = 0.75) -> float:
        tf, n = self._tf[i], len(self.clauses)
        dl = sum(tf.values())
        s = 0.0
        for w in q:
            if w not in tf:
                continue
            idf = math.log(1 + (n - self._df[w] + 0.5) / (self._df[w] + 0.5))
            s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * dl / self._avg))
        return s

    def retrieve(self, query: str, k: int = 3, require_tags: set[str] | None = None,
                 with_borrower_line: bool = False) -> list[Clause]:
        """Top-k clauses for the query, returned in document order (so composed messages read well)."""
        q = _tokens(query)
        scored = []
        for i, c in enumerate(self.clauses):
            if require_tags and not require_tags.issubset(set(c.tags)):
                continue
            if with_borrower_line and not c.borrower:
                continue
            s = self._score(q, i)
            if s > 0 or require_tags:
                scored.append((s, c))
        top = [c for _, c in sorted(scored, key=lambda x: -x[0])[:k]]
        return sorted(top, key=lambda c: c.order)


@lru_cache
def policy() -> PolicyStore:
    return PolicyStore.load()


def consequences(kind: str, partial: bool = False) -> list[Clause]:
    """Clauses a borrower must hear before a waiver / settlement is submitted."""
    query = f"{kind} approval stages consequence what happens" + (" partial payment shortfall" if partial else "")
    return policy().retrieve(query, k=4, require_tags={kind, "consequence"}, with_borrower_line=True)


def clauses_for_case(kind: str, partial: bool = False) -> list[Clause]:
    """All clauses that govern a waiver / settlement case (for tickets and the context pack)."""
    query = f"{kind} approval stages confirm submit consequence" + (" partial payment shortfall compliance" if partial else "")
    # Clauses tagged for a specific payment situation apply only in that situation.
    situational = {"partial", "full", "unconfirmed"}
    allowed = {"partial"} if partial else set()
    found = [c for c in policy().retrieve(query, k=8, require_tags={kind})
             if not (set(c.tags) & situational) or (set(c.tags) & allowed)]
    return sorted(found, key=lambda c: c.order)
