"""Per-turn trace spans for the Traces console (SA_TRACING=true; off by default).

Design rules (so tracing can never change agent behaviour):
* Nodes are wrapped, never edited. The wrapper returns the node's result untouched and
  re-raises the node's own exceptions; any failure inside the tracer itself is swallowed.
* Spans live outside graph state's data flow (a collector object, like ``gw``) and outside
  ``TurnResult.events``. They are written to their own table, ``trace_spans``.
* Inputs / outputs are small masked summaries, using the same masking as the audit log.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Callable

from servicing_agent.agent.tools.reconciliation import india_now

log = logging.getLogger(__name__)

TEXT_LIMIT = 300


def trace_id_for(conversation_id: str, message_id: str) -> str:
    """Opaque id for one turn. Hashed: the conversation id contains the borrower's mobile number."""
    return "tr_" + hashlib.sha256(f"{conversation_id}|{message_id}".encode()).hexdigest()[:16]


def _clip(text: str | None, mask: Callable) -> str | None:
    if text is None:
        return None
    return (mask(text) or "")[:TEXT_LIMIT]


def _intents(tasks) -> list[dict]:
    out = []
    for t in tasks or []:
        intent = getattr(t, "intent", None)
        out.append({"intent": getattr(intent, "value", intent), "confidence": getattr(t, "confidence", None)})
    return out


def summarize_input(s: dict, mask: Callable) -> dict:
    gw = s.get("gw")
    sess = s.get("session") or {}
    return {
        "text": _clip(s.get("text"), mask),
        "verified": bool(gw and gw.scope),
        "verification_stage": (sess.get("verification") or {}).get("stage"),
        "pending_tasks": len(sess.get("pending_tasks") or []),
        "has_pending_change": bool(sess.get("pending_change") or sess.get("pending_action")),
    }


def summarize_output(result: Any, mask: Callable) -> dict:
    if not isinstance(result, dict):
        return {}
    out: dict[str, Any] = {}
    if "text" in result:
        out["text"] = _clip(result.get("text"), mask)
    if result.get("tasks"):
        out["tasks"] = _intents(result["tasks"])
    for key in ("replies", "final_replies"):
        if result.get(key):
            out[key] = [{"template_id": getattr(r, "template_id", None), "text": _clip(getattr(r, "text", ""), mask)}
                        for r in result[key]]
    if result.get("events"):
        out["events"] = [e.get("type") for e in result["events"]]
    if result.get("tickets"):
        out["tickets"] = [{k: t.get(k) for k in ("ticket_id", "queue") if k in t} for t in result["tickets"]]
    if result.get("resolutions"):
        out["resolutions"] = list(result["resolutions"])
    if result.get("triage"):
        out["triage"] = result["triage"]
    if result.get("translation"):
        out["translation"] = {k: v for k, v in result["translation"].items() if k != "english"}
    if result.get("handoff_intent"):
        out["handoff_intent"] = getattr(result["handoff_intent"], "value", result["handoff_intent"])
    return out


class TurnTrace:
    """Collects the spans of one turn. Created by the runtime, read by the node wrapper."""

    def __init__(self, trace_id: str, conversation_id: str, mask: Callable):
        self.trace_id = trace_id
        self.conversation_id = conversation_id
        self.mask = mask
        self.spans: list[dict] = []
        self._t0 = time.perf_counter()
        self.started_at = india_now()

    def add(self, name: str, status: str, started_at, duration_ms: int, input_: dict, output: dict,
            tool_calls: list | None, error: str | None) -> None:
        self.spans.append({
            "trace_id": self.trace_id, "conversation_id": self.conversation_id, "seq": len(self.spans) + 1,
            "name": name, "status": status, "started_at": started_at, "duration_ms": duration_ms,
            "input": input_, "output": output, "tool_calls": tool_calls or None, "error": error,
        })

    def turn_span(self, status: str, input_: dict, output: dict, error: str | None = None) -> dict:
        path = [sp["name"] for sp in self.spans]
        return {
            "trace_id": self.trace_id, "conversation_id": self.conversation_id, "seq": 0, "name": "turn",
            "status": status, "started_at": self.started_at,
            "duration_ms": int((time.perf_counter() - self._t0) * 1000),
            "input": input_, "output": {**output, "path": path}, "tool_calls": None, "error": error,
        }


def wrap_node(name: str, fn: Callable) -> Callable:
    """Wrap a LangGraph node so it records a span. Behaviour of ``fn`` is untouched."""

    def wrapped(s):
        trace: TurnTrace | None = s.get("trace")
        if trace is None:
            return fn(s)
        started_at, t0 = india_now(), time.perf_counter()
        gw = s.get("gw")
        before = len(gw.events) if gw is not None else 0
        try:
            input_ = summarize_input(s, trace.mask)
        except Exception:  # noqa: BLE001 - tracing must never break a turn
            log.exception("trace input summary failed in %s", name)
            input_ = {}
        try:
            result = fn(s)
        except Exception as exc:
            _record(trace, name, "failed", started_at, t0, input_, {}, gw, before, f"{type(exc).__name__}: {exc}"[:500])
            raise
        _record(trace, name, "succeeded", started_at, t0, input_, result, gw, before, None)
        return result

    wrapped.__name__ = getattr(fn, "__name__", name)
    return wrapped


def _record(trace, name, status, started_at, t0, input_, result, gw, before, error) -> None:
    try:
        calls = None
        if gw is not None and len(gw.events) > before:
            calls = [{"type": e["type"], **(e.get("data") or {})} for e in gw.events[before:]]
        trace.add(name, status, started_at, int((time.perf_counter() - t0) * 1000), input_,
                  summarize_output(result, trace.mask), calls, error)
    except Exception:  # noqa: BLE001
        log.exception("trace span record failed in %s", name)


def make_trace_sink(session_factory) -> Callable[[list[dict]], None]:
    from servicing_agent.platform.db import TraceSpan

    def sink(spans: list[dict]) -> None:
        with session_factory.begin() as s:
            s.add_all(TraceSpan(**sp) for sp in spans)

    return sink
