"""The servicing policy document (config/policy/servicing_policy.md): rules, retrieval and wording.

Two jobs, done differently on purpose:

* **Deciding** (`decide`): decision clauses carry `decides / when / then` lines. Code computes a small,
  fixed set of facts (ledger verdict, authenticity, coverage...) and the first clause in the document whose
  condition matches decides the action. Exact matching, never similarity: the silent-fraud rule must fire
  every time, not "usually". Thresholds, queues and "tell the borrower or not" live in the document.
* **Explaining** (`retrieve`): hybrid search picks clauses to quote (borrower lines, word for word) and to
  cite on tickets and in the approval context pack. Score = ``alpha * BM25 (normalised) + (1 - alpha) *
  cosine(embedding)``; tags can be required as a hard filter. Without an embedder it is BM25 only.

Embedders are pluggable: fastembed (local ONNX, BAAI/bge-small-en-v1.5) or Sarvam /v2/embeddings when
enabled for the organisation. Regional text is translated to English upstream, so an English model is enough.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

POLICY_PATH = Path(__file__).resolve().parents[3] / "config" / "policy" / "servicing_policy.md"
_HEADING = re.compile(r"^##\s+([A-Z]{2,4}-\d{2})\s+·\s+(.+?)\s*$")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "is", "it", "in", "on", "for", "be", "my", "i", "if", "what",
         "how", "does", "do", "will", "can", "this", "that", "with", "as", "are", "by", "not", "no", "me"}

# The facts code computes for each situation. A clause using anything else fails at load (and in CI).
FACTS: dict[str, set[str]] = {
    "payment_claim": {"verdict", "authenticity", "same_borrower_loan"},
    "case_request": {"request", "payment", "coverage"},
}
ACTIONS: dict[str, set[str]] = {
    "payment_claim": {"confirm", "suggest_switch", "escalate", "wait_then_escalate"},
    "case_request": {"wait_for_payment", "not_needed", "confirm_then_submit"},
}


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


# ---------------- rule language: tiny on purpose ----------------
# condition := term ("and" term)* ; term := fact | "not" fact | fact op value ; op := == != in "not in"

_TERM = re.compile(r"^(?P<fact>[a-z_]+)\s*(?:(?P<op>==|!=|not in|in)\s*(?P<val>.+))?$")


def _value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [_value(x) for x in raw[1:-1].split(",") if x.strip()]
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _parse_condition(text: str) -> list[tuple[str, str, Any]]:
    terms = []
    for part in re.split(r"\s+and\s+", text.strip()):
        neg = part.startswith("not ") and " in " not in part
        m = _TERM.match(part[4:].strip() if neg else part.strip())
        if not m:
            raise ValueError(f"bad condition: {part!r}")
        op = m.group("op") or ("falsy" if neg else "truthy")
        terms.append((m.group("fact"), op, _value(m.group("val")) if m.group("val") else None))
    return terms


def _parse_then(text: str) -> dict[str, Any]:
    out = {}
    for part in text.split(";"):
        if part.strip():
            k, _, v = part.partition("=")
            out[k.strip()] = _value(v)
    return out


def _holds(term: tuple[str, str, Any], facts: dict) -> bool:
    fact, op, val = term
    have = facts.get(fact)
    if op == "truthy":
        return bool(have)
    if op == "falsy":
        return not have
    if op == "==":
        return have == val
    if op == "!=":
        return have != val
    if op == "in":
        return have in val
    if op == "not in":
        return have not in val
    return False


@dataclass(frozen=True)
class Clause:
    id: str
    title: str
    tags: tuple[str, ...]
    borrower: str | None
    rule: str
    order: int
    decides: str | None = None
    when: tuple = ()                    # tuple of alternatives; each a list of terms (all must hold)
    then: tuple = ()                    # tuple of (key, value) pairs

    @property
    def action(self) -> dict[str, Any]:
        return dict(self.then)

    def matches(self, facts: dict) -> bool:
        return any(all(_holds(t, facts) for t in alt) for alt in self.when)

    def ref(self) -> dict:
        return {"id": self.id, "title": self.title, "rule": self.rule, "borrower": self.borrower}


@dataclass
class Decision:
    clause: Clause
    action: dict[str, Any]
    facts: dict[str, Any]

    def record(self, version: str) -> dict:
        return {"clause": self.clause.id, "title": self.clause.title, "action": self.action,
                "facts": self.facts, "policy_version": version}


# ---------------- embeddings ----------------

class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedEmbedder:
    """Local ONNX model, no network after the first download."""

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding  # optional dependency: pip install -e ".[embeddings]"
        cache = POLICY_PATH.parents[2] / ".cache" / "fastembed"     # survives reboots (git-ignored)
        self.model = TextEmbedding(model, cache_dir=str(cache))
        self.name = f"fastembed:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self.model.embed(texts)]


class SarvamEmbedder:
    """Sarvam /v2/embeddings (EmbeddingGemma 2, OpenAI-shaped). Needs Provisioned Throughput access."""

    def __init__(self, api_key: str, model: str = "embeddinggemma-2", base_url: str = "https://api.sarvam.ai"):
        import httpx
        self.client = httpx.Client(base_url=base_url, timeout=20,
                                   headers={"api-subscription-key": api_key, "Authorization": f"Bearer {api_key}"})
        self.model = model
        self.name = f"sarvam:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        r = self.client.post("/v2/embeddings", json={"model": self.model, "input": texts})
        r.raise_for_status()
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def make_embedder(kind: str, model: str | None = None, api_key: str | None = None) -> Embedder | None:
    """"fastembed" | "sarvam" | "off". Any failure -> None (BM25 only), logged, never fatal."""
    try:
        if kind == "fastembed":
            return FastEmbedEmbedder(model or "BAAI/bge-small-en-v1.5")
        if kind == "sarvam" and api_key:
            e = SarvamEmbedder(api_key, model or "embeddinggemma-2")
            e.embed(["ping"])                       # fail fast if the org has no access
            return e
    except Exception as exc:  # noqa: BLE001
        log.warning("policy embeddings %s unavailable (%s); using keyword search only", kind, exc)
    return None


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# ---------------- store ----------------

@dataclass
class Scored:
    clause: Clause
    bm25: float
    cosine: float
    score: float


@dataclass
class PolicyStore:
    clauses: list[Clause]
    version: str = ""
    embedder: Embedder | None = None
    alpha: float = 0.5                      # weight of BM25 vs embeddings in the hybrid score
    _tf: list[Counter] = field(default_factory=list, repr=False)
    _df: Counter = field(default_factory=Counter, repr=False)
    _avg: float = 0.0
    _vecs: list[list[float]] | None = field(default=None, repr=False)

    def __post_init__(self):
        self._tf = [Counter(_tokens(f"{c.title} {' '.join(c.tags)} {' '.join(c.tags)} {c.rule}")) for c in self.clauses]
        for tf in self._tf:
            self._df.update(set(tf))
        self._avg = sum(sum(tf.values()) for tf in self._tf) / max(1, len(self._tf))

    @classmethod
    def load(cls, path: Path = POLICY_PATH, embedder: Embedder | None = None) -> "PolicyStore":
        text = path.read_text(encoding="utf-8")
        version = (re.search(r"^Version:\s*([^.]+)", text, re.M) or [None, ""])[1].strip()
        clauses, cur, order = [], None, 0

        def close(cur, order):
            clause = Clause(cur["id"], cur["title"], tuple(cur["tags"]), cur["borrower"], " ".join(cur["rule"]).strip(),
                            order, cur["decides"], tuple(tuple(_parse_condition(w)) for w in cur["when"]),
                            tuple(_parse_then(cur["then"]).items()) if cur["then"] else ())
            _validate(clause)
            return clause

        for line in text.splitlines() + ["## END-00 · end"]:
            m = _HEADING.match(line)
            if m:
                if cur:
                    clauses.append(close(cur, order))
                    order += 1
                cur = {"id": m.group(1), "title": m.group(2), "tags": [], "borrower": None, "rule": [],
                       "decides": None, "when": [], "then": None}
            elif cur is not None:
                for key in ("tags", "borrower", "decides", "when", "then"):
                    if line.startswith(f"{key}:"):
                        val = line[len(key) + 1:].strip()
                        if key == "tags":
                            cur["tags"] = [t.strip().lower() for t in val.split(",") if t.strip()]
                        elif key == "when":
                            cur["when"].append(val)
                        else:
                            cur[key] = val
                        break
                else:
                    if line.strip():
                        cur["rule"].append(line.strip())
        return cls([c for c in clauses if c.id != "END-00"], version, embedder)

    def get(self, clause_id: str) -> Clause:
        return next(c for c in self.clauses if c.id == clause_id)

    # ---- deciding (exact)
    def decide(self, situation: str, facts: dict) -> Decision | None:
        for c in self.clauses:
            if c.decides == situation and c.matches(facts):
                return Decision(c, c.action, dict(facts))
        return None

    # ---- explaining (hybrid search)
    def _bm25(self, q: list[str], i: int, k1: float = 1.4, b: float = 0.75) -> float:
        tf, n = self._tf[i], len(self.clauses)
        dl = sum(tf.values())
        s = 0.0
        for w in q:
            if w not in tf:
                continue
            idf = math.log(1 + (n - self._df[w] + 0.5) / (self._df[w] + 0.5))
            s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * dl / self._avg))
        return s

    def _clause_vectors(self) -> list[list[float]] | None:
        if self.embedder is None:
            return None
        if self._vecs is None:
            docs = [f"{c.title}. {c.borrower or ''} {c.rule} ({', '.join(c.tags)})" for c in self.clauses]
            try:
                self._vecs = [_unit(v) for v in self.embedder.embed(docs)]
            except Exception as exc:  # noqa: BLE001
                log.warning("policy embeddings failed (%s); keyword search only", exc)
                self.embedder = None
                return None
        return self._vecs

    def search(self, query: str, require_tags: set[str] | None = None,
               with_borrower_line: bool = False) -> list[Scored]:
        """All candidate clauses with their BM25, cosine and hybrid scores, best first."""
        q = _tokens(query)
        idx = [i for i, c in enumerate(self.clauses)
               if (not require_tags or require_tags.issubset(set(c.tags))) and (not with_borrower_line or c.borrower)]
        bm = {i: self._bm25(q, i) for i in idx}
        top_bm = max(bm.values(), default=0) or 1.0
        vecs = self._clause_vectors()
        qv = None
        if vecs is not None and query.strip():
            try:
                qv = _unit(self.embedder.embed([query])[0])
            except Exception as exc:  # noqa: BLE001
                log.warning("query embedding failed (%s)", exc)
        out = []
        for i in idx:
            cos = max(0.0, _cos(qv, vecs[i])) if qv is not None else 0.0
            score = (self.alpha * bm[i] / top_bm + (1 - self.alpha) * cos) if qv is not None else bm[i] / top_bm
            out.append(Scored(self.clauses[i], bm[i], cos, score))
        return sorted(out, key=lambda s: -s.score)

    def retrieve(self, query: str, k: int = 3, require_tags: set[str] | None = None,
                 with_borrower_line: bool = False, min_score: float = 0.0) -> list[Clause]:
        """Top-k clauses, returned in document order (so composed messages read well)."""
        hits = self.search(query, require_tags, with_borrower_line)
        if not require_tags:
            hits = [h for h in hits if h.score > min_score and (h.bm25 > 0 or h.cosine > 0)]
        return sorted((h.clause for h in hits[:k]), key=lambda c: c.order)


def _validate(c: Clause) -> None:
    if not c.decides:
        # A clause may carry parameters (then: key=value) without deciding anything, e.g. limits.
        if c.when:
            raise ValueError(f"{c.id}: when without decides")
        return
    if c.decides not in FACTS:
        raise ValueError(f"{c.id}: unknown situation {c.decides!r}")
    if not c.when:
        raise ValueError(f"{c.id}: decision clause without when")
    for alt in c.when:
        for fact, _, _ in alt:
            if fact not in FACTS[c.decides]:
                raise ValueError(f"{c.id}: unknown fact {fact!r} for {c.decides}")
    action = c.action.get("action")
    if action not in ACTIONS[c.decides]:
        raise ValueError(f"{c.id}: unknown action {action!r} for {c.decides}")


# ---------------- process-wide store ----------------

_STORE: PolicyStore | None = None
_EMBEDDER: Embedder | None = None


def configure(embedder: Embedder | None) -> None:
    """Set the embedder (build_runtime does this from settings) and reload the policy."""
    global _STORE, _EMBEDDER
    _EMBEDDER, _STORE = embedder, None


def policy() -> PolicyStore:
    global _STORE
    if _STORE is None:
        _STORE = PolicyStore.load(POLICY_PATH, embedder=_EMBEDDER)   # module path read at call time (tests)
    return _STORE


def consequences(kind: str, partial: bool = False) -> list[Clause]:
    """Clauses a borrower must hear before a waiver / settlement is submitted."""
    query = f"{kind} approval stages consequence what happens" + (" partial payment shortfall" if partial else "")
    return policy().retrieve(query, k=4, require_tags={kind, "consequence"}, with_borrower_line=True)


def clauses_for_case(kind: str, partial: bool = False) -> list[Clause]:
    """All clauses that govern a waiver / settlement case (for tickets and the context pack)."""
    query = f"{kind} approval stages confirm submit consequence" + (" partial payment shortfall compliance" if partial else "")
    situational = {"partial", "full", "unconfirmed"}       # apply only in that payment situation
    allowed = {"partial"} if partial else set()
    found = [c for c in policy().retrieve(query, k=8, require_tags={kind})
             if not (set(c.tags) & situational) or (set(c.tags) & allowed)]
    return sorted(found, key=lambda c: c.order)
