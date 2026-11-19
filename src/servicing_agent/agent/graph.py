"""The conversation turn as a bounded LangGraph state machine.

    screen ─> normalize ─┬─> handoff ───────────────────────────────┐
                         ├─> confirm_change ────────────────────────┤
                         ├─> verify ──(verified, pending tasks)──> execute
                         │          └─(otherwise)───────────────────┤
                         └─> plan ─┬─> start_verification ──────────┤
                                   ├─> clarify ─────────────────────┤
                                   └─> execute ─────────────────────┴─> output_guard -> END

normalize translates regional / code-mixed text to checked English, only when needed.
LLM decision points: perception (upstream, T3), normalize and plan. Everything with
consequences is code. Session state lives in the ConversationStore, not in the
graph; the graph is the executor for one turn.
"""

from __future__ import annotations

import hashlib
import operator
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from servicing_agent.agent import handlers, verification
from servicing_agent.agent.gateway import NotVerified, PolicyGateway
from servicing_agent.agent.normalizer import needs_translation
from servicing_agent.agent.output_guard import check
from servicing_agent.agent.planner import RulePlanner
from servicing_agent.agent.screening import screen
from servicing_agent.agent.templates import render
from servicing_agent.config import Settings
from servicing_agent.contracts.events import EventType
from servicing_agent.contracts.state import (
    ACCOUNT_INTENTS, AuthScope, InboundMessage, Intent, PendingChange, Reply, Task, TurnResult, VerificationState,
)
from servicing_agent.data.identifiers import extract_account_number

YES_RE = re.compile(r"^\s*(yes|y|haan|ha|ok|okay|confirm|sure|हाँ)\b", re.IGNORECASE)
NO_RE = re.compile(r"^\s*(no|n|nahi|nahin|cancel|don'?t|नहीं)\b", re.IGNORECASE)


class TurnState(TypedDict, total=False):
    conversation_id: str
    inbound: InboundMessage
    text: str
    session: dict[str, Any]
    handoff_intent: Intent | None
    original_text: str                  # set when the turn was translated; `text` is then the English
    translation: dict[str, Any]
    tasks: list[Task]
    gw: PolicyGateway
    replies: Annotated[list[Reply], operator.add]
    final_replies: list[Reply]          # set once, by output_guard
    tickets: Annotated[list[dict], operator.add]
    events: Annotated[list[dict], operator.add]
    resolutions: Annotated[list[str], operator.add]


def build_graph(runtime) -> Any:
    settings: Settings = runtime.settings

    def _ctx(s: TurnState) -> handlers.Ctx:
        return handlers.Ctx(gw=s["gw"], settings=settings, lang=s["session"].get("lang", "en"),
                            conversation_id=s["conversation_id"], message_id=s["inbound"].message_id,
                            summary_text=s["text"], answerer=getattr(runtime, "answerer", None))

    # ---------------- nodes
    def screen_node(s: TurnState):
        msg = s["inbound"]
        # Typed text, voice transcripts and image text, in the order the borrower sent them.
        text = "\n".join(msg.ordered_text_parts()).strip()
        res = screen(text)
        events = []
        if res.injection:
            events.append({"type": EventType.INJECTION_DETECTED.value,
                           "data": {"source": msg.type, "snippets": res.injection_snippets}})
        return {"text": res.cleaned_text, "handoff_intent": res.handoff_intent, "events": events}

    rules = RulePlanner()

    def normalize_node(s: TurnState):
        """Regional / code-mixed text -> checked English, before routing and planning."""
        normalizer = getattr(runtime, "normalizer", None)
        sess = s["session"]
        stage = VerificationState(**sess["verification"]).stage
        text = s.get("text", "")
        if (normalizer is None or s.get("handoff_intent")
                or stage in ("awaiting_account", "awaiting_otp", "locked")
                or (sess.get("pending_change") and (YES_RE.search(text) or NO_RE.search(text)))
                or not needs_translation(text, rules)):
            return {}
        t = normalizer.translate(text, s["inbound"].received_at)
        if not t.ok:
            return {"events": [{"type": EventType.TRANSLATION_FAILED.value, "data": {"error": t.error}}]}
        info = {"language": t.language, "model": t.model, "latency_ms": t.latency_ms,
                "removed_numbers": len(t.removed_numbers), "verified_amounts": t.verified_amounts,
                "resolved_dates": t.resolved_dates}
        rescreen = screen(t.english)              # injections or distress hidden in another language
        events = [{"type": EventType.TRANSLATED.value, "data": info}]
        if rescreen.injection:
            events.append({"type": EventType.INJECTION_DETECTED.value,
                           "data": {"source": "translation", "snippets": rescreen.injection_snippets}})
        return {"text": rescreen.cleaned_text, "original_text": text,
                "translation": {**info, "english": t.english},
                "handoff_intent": rescreen.handoff_intent, "events": events}

    def route_after_screen(s: TurnState) -> str:
        sess = s["session"]
        vs = VerificationState(**sess["verification"])
        if s.get("handoff_intent"):
            return "handoff"
        if sess.get("pending_change") and (YES_RE.search(s["text"]) or NO_RE.search(s["text"])):
            return "confirm_change"
        if vs.stage in ("awaiting_account", "awaiting_otp", "locked"):
            return "verify"
        acc = extract_account_number(s["text"])
        scope = s["gw"].scope
        if acc and (scope is None or acc != scope.account_id):
            return "verify"
        return "plan"

    def handoff_node(s: TurnState):
        task = Task(intent=s["handoff_intent"], confidence=1.0)
        out = handlers.run_task(_ctx(s), task)
        ev = {"type": EventType.HANDOFF.value, "data": {"intent": task.intent.value}}
        return {"tasks": [task], "replies": out.replies, "tickets": out.tickets, "events": [ev],
                "resolutions": [out.resolution]}

    def confirm_change_node(s: TurnState):
        sess = s["session"]
        pending = PendingChange(**sess["pending_change"])
        yes = bool(YES_RE.search(s["text"]))
        out = handlers.confirm_change(_ctx(s), pending, yes)
        sess["pending_change"] = None
        intent = Intent(pending.request.get("type", "waiver") + "_request")
        return {"tasks": [Task(intent=intent)], "replies": out.replies, "resolutions": [out.resolution]}

    def verify_node(s: TurnState):
        sess = s["session"]
        vs = VerificationState(**sess["verification"])
        out = verification.handle(s["text"], s["inbound"].sender, vs, s["gw"].scope, runtime.tools, settings,
                                  sess.get("lang", "en"))
        sess["verification"] = out.state.model_dump(mode="json")
        s["gw"].scope = out.scope
        sess["scope"] = out.scope.model_dump(mode="json") if out.scope else None
        result: dict = {"events": out.events}
        tickets = []
        if out.security_alert:
            tickets.append(s["gw"].ticket("security_alert", {"reason": "repeated failed verification"}))
        if out.just_verified:
            pending = [Task(**t) for t in sess.get("pending_tasks", [])]
            sess["pending_tasks"] = []
            replies = [] if pending else [render("verified_ok", sess.get("lang", "en"))]
            result.update(tasks=pending, replies=replies, tickets=tickets, resolutions=[])
        else:
            result.update(tasks=[], replies=out.replies, tickets=tickets, resolutions=["pending_verification"])
        return result

    def route_after_verify(s: TurnState) -> str:
        return "execute" if s.get("tasks") else "output_guard"

    def plan_node(s: TurnState):
        tasks = runtime.planner.plan(s["text"], s["inbound"].all_media())
        return {"tasks": tasks}

    def route_after_plan(s: TurnState) -> str:
        tasks = s["tasks"]
        if all(t.intent == Intent.UNKNOWN or t.confidence < 0.6 for t in tasks):
            return "clarify"
        if any(t.intent in ACCOUNT_INTENTS for t in tasks) and s["gw"].scope is None:
            return "start_verification"
        return "execute"

    def clarify_node(s: TurnState):
        sess = s["session"]
        sess["clarify_count"] = sess.get("clarify_count", 0) + 1
        if sess["clarify_count"] >= 2:
            sess["clarify_count"] = 0
            out = handlers._route(_ctx(s), "unknown", Task(intent=Intent.UNKNOWN))
            return {"replies": out.replies, "tickets": out.tickets, "resolutions": ["routed"]}
        return {"replies": [render("clarify", sess.get("lang", "en"))], "resolutions": ["clarify"]}

    def start_verification_node(s: TurnState):
        sess = s["session"]
        sess["pending_tasks"] = [t.model_dump(mode="json") for t in s["tasks"]]
        vs = VerificationState(**sess["verification"])
        vs.stage = "awaiting_account"
        sess["verification"] = vs.model_dump(mode="json")
        return {"replies": [render("ask_account", sess.get("lang", "en"))],
                "events": [{"type": EventType.VERIFICATION_STARTED.value, "data": {"stage": "ask_account"}}],
                "resolutions": ["pending_verification"]}

    def execute_node(s: TurnState):
        sess = s["session"]
        ctx = _ctx(s)
        replies, tickets, resolutions = [], [], []
        for task in s["tasks"]:
            try:
                out = handlers.run_task(ctx, task)
            except NotVerified:
                replies.append(render("ask_account", ctx.lang))
                resolutions.append("pending_verification")
                continue
            replies += out.replies
            tickets += out.tickets
            resolutions.append(out.resolution)
            if out.pending_change:
                sess["pending_change"] = out.pending_change.model_dump(mode="json")
        sess["clarify_count"] = 0
        return {"replies": replies, "tickets": tickets, "resolutions": resolutions}

    def output_guard_node(s: TurnState):
        gw: PolicyGateway = s["gw"]
        verified_account = gw.scope.account_id if gw.scope else None
        constants = {settings.helpline}
        safe, events = [], []
        for r in s.get("replies", []):
            g = check(r.text, verified_account=verified_account, allowed_numbers=gw.allowed_numbers,
                      allowed_strings=gw.allowed_strings, constants=constants)
            if g.ok:
                safe.append(r)
            else:
                events.append({"type": EventType.OUTPUT_BLOCKED.value,
                               "data": {"reason": g.reason, "detail": g.detail, "template_id": r.template_id}})
                safe.append(render("safe_fallback", s["session"].get("lang", "en")))
        return {"final_replies": safe, "events": events}

    g = StateGraph(TurnState)
    for name, fn in [("screen", screen_node), ("normalize", normalize_node),
                     ("handoff", handoff_node), ("confirm_change", confirm_change_node),
                     ("verify", verify_node), ("plan", plan_node), ("clarify", clarify_node),
                     ("start_verification", start_verification_node), ("execute", execute_node),
                     ("output_guard", output_guard_node)]:
        g.add_node(name, fn)
    g.set_entry_point("screen")
    g.add_edge("screen", "normalize")
    g.add_conditional_edges("normalize", route_after_screen,
                            {k: k for k in ["handoff", "confirm_change", "verify", "plan"]})
    g.add_conditional_edges("verify", route_after_verify, {"execute": "execute", "output_guard": "output_guard"})
    g.add_conditional_edges("plan", route_after_plan,
                            {k: k for k in ["clarify", "start_verification", "execute"]})
    for n in ["handoff", "confirm_change", "clarify", "start_verification", "execute"]:
        g.add_edge(n, "output_guard")
    g.add_edge("output_guard", END)
    return g.compile()


class AgentRuntime:
    """Wires tools, case service, planner and conversation store around the graph."""

    # How many processed message ids to remember per conversation for duplicate detection.
    DEDUPE_WINDOW = 200

    def __init__(self, *, settings: Settings, tools, cases, planner, conversations, audit_sink=None, claims=None):
        self.settings = settings
        self.tools = tools
        self.cases = cases
        self.planner = planner
        self.conversations = conversations
        self.audit_sink = audit_sink
        self.claims = claims
        self.normalizer = None              # set by build_runtime when SA_NORMALIZER=sarvam
        self.answerer = None                # SarvamAnswerer for free-form loan questions (same switch)
        self.evidence = None                # EvidenceRecorder: full media/extraction/translation rows
        self.graph = build_graph(self)

    def _record_evidence(self, msg: InboundMessage, state: dict) -> None:
        """Full, unmasked perception output and translations -> media / extractions tables.

        Never blocks the reply: an audit write failure is logged, not raised.
        """
        if not self.evidence:
            return
        try:
            for ev in msg.all_media():
                mid = ev.source_message_id or msg.message_id
                self.evidence.record_media_evidence(ev, fallback_message_id=msg.message_id,
                                                    media_handle=msg.media_handle_for(mid))
            if state.get("translation"):
                self.evidence.record_translation(msg.message_id, state["translation"], state.get("original_text", ""))
        except Exception:                    # noqa: BLE001 - audit must not break the conversation
            import logging
            logging.getLogger(__name__).exception("evidence audit write failed for %s", msg.message_id)

    def conversation_id_for(self, sender: str) -> str:
        from servicing_agent.data.identifiers import normalize_mobile
        return f"wa:{normalize_mobile(sender)}"

    def run_turn(self, msg: InboundMessage, conversation_id: str | None = None) -> TurnResult:
        cid = conversation_id or self.conversation_id_for(msg.sender)
        with self.conversations.lock(cid):
            convo = self.conversations.load(cid)
            # Duplicate delivery (WhatsApp retries a webhook, a client resends a request): same
            # message id -> return the first result, marked duplicate. Nothing runs twice: no second
            # claim, ticket, case or audit row, and senders must not resend the replies.
            processed: dict = convo.setdefault("processed", {})
            if msg.message_id in processed:
                return TurnResult(**{**processed[msg.message_id], "duplicate": True})
            sess = convo["session"]
            now = datetime.utcnow()
            scope = AuthScope(**sess["scope"]) if sess.get("scope") else None
            if scope and not verification.scope_valid(scope, now):
                scope, sess["scope"] = None, None
                vs = VerificationState(**sess["verification"])
                vs.stage = "none"
                sess["verification"] = vs.model_dump(mode="json")
            elif scope:
                scope = scope.model_copy(update={"expires_at": now + verification.SESSION_TTL})
                sess["scope"] = scope.model_dump(mode="json")

            gw = PolicyGateway(self.tools, self.cases, scope, cid, self.settings.settlement_window_hours,
                               claims=self.claims, message_id=msg.message_id)
            inbound_ev = {"type": EventType.INBOUND.value,
                          # Masked: audit_events is the broadly readable log (OTPs, UTRs, mobiles).
                          # Exact words live in the access-restricted `extractions` table.
                          "data": {"message_id": msg.message_id, "type": msg.type, "text": mask_text(msg.text),
                                   "verified": scope is not None}}
            state = self.graph.invoke(
                {"conversation_id": cid, "inbound": msg, "session": sess, "gw": gw,
                 "replies": [], "tickets": [], "events": [inbound_ev], "resolutions": []},
                config=trace_config(cid, msg, scope is not None, self.settings.planner),
            )

            sess["scope"] = gw.scope.model_dump(mode="json") if gw.scope else None
            replies = state["final_replies"]
            normalized = normalized_view(msg, state, verified_before=scope is not None,
                                         verified_after=gw.scope is not None)
            norm_ev = {"type": EventType.TURN_NORMALIZED.value, "data": normalized}
            events = state["events"][:1] + [norm_ev] + gw.events + state["events"][1:]
            for r in replies:
                events.append({"type": EventType.OUTBOUND.value,
                               "data": {"template_id": r.template_id, "text": mask_text(r.text),
                                        "verified": gw.scope is not None}})
            stamp = datetime.utcnow().isoformat(timespec="seconds")
            for e in events:
                e.setdefault("at", stamp)
            convo["events"] += events
            convo["tickets"] += state.get("tickets", [])
            convo["transcript"].append({"role": "borrower", "message_id": msg.message_id, "type": msg.type,
                                        "text": msg.text, "at": stamp})
            convo["transcript"] += [{"role": "agent", "text": r.text, "template_id": r.template_id, "at": stamp}
                                    for r in replies]
            if self.audit_sink:
                self.audit_sink(cid, events)
            self._record_evidence(msg, state)

            res = state.get("resolutions", [])
            resolution = ("routed" if "routed" in res else "pending_verification" if "pending_verification" in res
                          else "clarify" if "clarify" in res else "auto" if res else "open")
            result = TurnResult(conversation_id=cid, replies=replies, verified=gw.scope is not None,
                                intents=[t.intent for t in state.get("tasks", [])], tickets=state.get("tickets", []),
                                events=events, resolution=resolution, normalized=normalized)
            dump = result.model_dump(mode="json")
            for mid in msg.batched_message_ids or [msg.message_id]:
                processed[mid] = dump
            while len(processed) > self.DEDUPE_WINDOW:
                processed.pop(next(iter(processed)))
            return result


_UTR_IN_TEXT = re.compile(r"(?<!\d)\d{12}(?!\d)")
_MOBILE_IN_TEXT = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
_OTP_IN_TEXT = re.compile(r"(?<!\d)\d{6}(?!\d)")


def mask_utr(utr: str | None) -> str | None:
    return f"••••{utr[-4:]}" if utr else None


def mask_text(text: str | None) -> str | None:
    """Audit-safe text: UTRs and mobiles keep their last 4 digits, 6-digit codes (OTPs) are hidden."""
    if not text:
        return text
    text = _UTR_IN_TEXT.sub(lambda m: mask_utr(m.group(0)), text)
    text = _MOBILE_IN_TEXT.sub(lambda m: f"••••••{m.group(0)[-4:]}", text)
    return _OTP_IN_TEXT.sub("••••••", text)


def normalized_view(msg: InboundMessage, state: dict, *, verified_before: bool, verified_after: bool) -> dict:
    """What the agent understood this turn, for the audit log and replay tooling.

    Merged text is the screened text the planner saw (injected lines removed). Media items keep
    their extraction quality signals; UTRs, mobiles and OTP codes are masked.
    """
    media = []
    for ev in msg.all_media():
        media.append({
            "kind": ev.kind, "source_message_id": ev.source_message_id, "provider": ev.provider,
            "language": ev.language, "confidence": ev.confidence, "error": ev.error,
            "transcript": mask_text(ev.transcript), "original_transcript": mask_text(ev.original_transcript),
            "utr": mask_utr(ev.utr), "amount": ev.amount, "txn_date": ev.txn_date, "txn_time": ev.txn_time,
            "status": ev.status, "low_confidence_fields": ev.low_confidence_fields,
        })
    tasks = []
    for t in state.get("tasks", []):
        slots = {k: v for k, v in t.slots.items() if v not in (None, [], "")}
        if "utr" in slots:
            slots["utr"] = mask_utr(slots["utr"])
        if "borrower_text" in slots:
            slots["borrower_text"] = mask_text(slots["borrower_text"])
        tasks.append({"intent": t.intent.value, "confidence": t.confidence, "slots": slots})
    injected = [e for e in state.get("events", []) if e["type"] == EventType.INJECTION_DETECTED.value]
    return {
        "message_ids": msg.batched_message_ids or [msg.message_id],
        "message_count": len(msg.batched_message_ids) or 1,
        "raw_text": mask_text(msg.text),
        "segments": [{"message_id": seg.get("message_id"), "type": seg.get("type"), "text": mask_text(seg.get("text"))}
                     for seg in msg.segments],
        "original_text": mask_text(state.get("original_text")),     # set only when translated
        "translation": ({k: (mask_text(v) if k == "english" else v) for k, v in state["translation"].items()}
                        if state.get("translation") else None),
        "merged_text": mask_text(state.get("text")),                # what the planner read
        "media": media,
        "injection_removed": bool(injected),
        "handoff": state.get("handoff_intent").value if state.get("handoff_intent") else None,
        "tasks": tasks,
        "verified_before": verified_before,
        "verified_after": verified_after,
    }


def trace_config(cid: str, msg: InboundMessage, verified: bool, planner: str) -> dict:
    """LangSmith labels for one turn. Only used when LANGSMITH_TRACING=true.

    No phone numbers or account numbers here: the conversation id (which contains the mobile)
    is hashed. Keep LANGSMITH_HIDE_INPUTS/OUTPUTS=true until PII masking is in place.
    """
    media = msg.all_media()
    return {
        "run_name": "servicing_turn",
        "tags": [f"type:{msg.type}", f"planner:{planner}", "verified" if verified else "unverified"],
        "metadata": {
            "conversation_hash": hashlib.sha256(cid.encode()).hexdigest()[:16],
            "message_type": msg.type,
            "media_kinds": [ev.kind for ev in media],
            "media_errors": [ev.error for ev in media if ev.error],
            "batched_messages": len(msg.batched_message_ids) or 1,
        },
    }


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:12]}"
