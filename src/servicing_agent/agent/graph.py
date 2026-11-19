"""The conversation turn as a bounded LangGraph state machine.

    screen ─┬─> handoff ───────────────────────────────┐
            ├─> confirm_change ────────────────────────┤
            ├─> verify ──(verified, pending tasks)──> execute
            │          └─(otherwise)───────────────────┤
            └─> plan ─┬─> start_verification ──────────┤
                      ├─> clarify ─────────────────────┤
                      └─> execute ─────────────────────┴─> output_guard -> END

LLM decision points: perception (upstream, T3) and plan. Everything with
consequences is code. Session state lives in the ConversationStore, not in the
graph; the graph is the executor for one turn.
"""

from __future__ import annotations

import operator
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from servicing_agent.agent import handlers, verification
from servicing_agent.agent.gateway import NotVerified, PolicyGateway
from servicing_agent.agent.output_guard import check
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
                            summary_text=s["text"])

    # ---------------- nodes
    def screen_node(s: TurnState):
        msg = s["inbound"]
        parts = [msg.text or ""]
        if msg.media:
            parts += [msg.media.transcript or "", msg.media.ocr_text or ""]
        text = " ".join(p for p in parts if p).strip()
        res = screen(text)
        events = []
        if res.injection:
            events.append({"type": EventType.INJECTION_DETECTED.value,
                           "data": {"source": msg.type, "snippets": res.injection_snippets}})
        return {"text": res.cleaned_text, "handoff_intent": res.handoff_intent, "events": events}

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
        tasks = runtime.planner.plan(s["text"], s["inbound"].media)
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
    for name, fn in [("screen", screen_node), ("handoff", handoff_node), ("confirm_change", confirm_change_node),
                     ("verify", verify_node), ("plan", plan_node), ("clarify", clarify_node),
                     ("start_verification", start_verification_node), ("execute", execute_node),
                     ("output_guard", output_guard_node)]:
        g.add_node(name, fn)
    g.set_entry_point("screen")
    g.add_conditional_edges("screen", route_after_screen,
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

    def __init__(self, *, settings: Settings, tools, cases, planner, conversations, audit_sink=None):
        self.settings = settings
        self.tools = tools
        self.cases = cases
        self.planner = planner
        self.conversations = conversations
        self.audit_sink = audit_sink
        self.graph = build_graph(self)

    def conversation_id_for(self, sender: str) -> str:
        from servicing_agent.data.identifiers import normalize_mobile
        return f"wa:{normalize_mobile(sender)}"

    def run_turn(self, msg: InboundMessage, conversation_id: str | None = None) -> TurnResult:
        cid = conversation_id or self.conversation_id_for(msg.sender)
        with self.conversations.lock(cid):
            convo = self.conversations.load(cid)
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

            gw = PolicyGateway(self.tools, self.cases, scope, cid, self.settings.settlement_window_hours)
            inbound_ev = {"type": EventType.INBOUND.value,
                          "data": {"message_id": msg.message_id, "type": msg.type, "text": msg.text,
                                   "verified": scope is not None}}
            state = self.graph.invoke({"conversation_id": cid, "inbound": msg, "session": sess, "gw": gw,
                                       "replies": [], "tickets": [], "events": [inbound_ev], "resolutions": []})

            sess["scope"] = gw.scope.model_dump(mode="json") if gw.scope else None
            replies = state["final_replies"]
            events = state["events"][:1] + gw.events + state["events"][1:]
            for r in replies:
                events.append({"type": EventType.OUTBOUND.value,
                               "data": {"template_id": r.template_id, "text": r.text,
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

            res = state.get("resolutions", [])
            resolution = ("routed" if "routed" in res else "pending_verification" if "pending_verification" in res
                          else "clarify" if "clarify" in res else "auto" if res else "open")
            return TurnResult(conversation_id=cid, replies=replies, verified=gw.scope is not None,
                              intents=[t.intent for t in state.get("tasks", [])], tickets=state.get("tickets", []),
                              events=events, resolution=resolution)


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:12]}"
