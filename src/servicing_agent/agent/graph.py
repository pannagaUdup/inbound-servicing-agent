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
import logging
import operator
import re
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from servicing_agent.platform.session_context import session_key
from servicing_agent.platform.tracing import TurnTrace, trace_id_for, wrap_node
from servicing_agent.agent import handlers, verification
from servicing_agent.agent.gateway import NotVerified, PolicyGateway
from servicing_agent.agent.normalizer import needs_translation
from servicing_agent.agent.output_guard import check
from servicing_agent.agent.policy import policy
from servicing_agent.agent.tools.reconciliation import ist_stamp
from servicing_agent.agent.reply_writer import REWRITABLE
from servicing_agent.agent.planner import RulePlanner
from servicing_agent.agent.screening import screen
from servicing_agent.agent.templates import render
from servicing_agent.agent.triage import Triage, triage
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
    triage: dict[str, Any]
    followups: Annotated[list[dict], operator.add]
    trace: Any                          # TurnTrace collector when SA_TRACING=true, else absent


def build_graph(runtime) -> Any:
    settings: Settings = runtime.settings

    def _ctx(s: TurnState, tri: Triage | None = None) -> handlers.Ctx:
        return handlers.Ctx(gw=s["gw"], settings=settings, lang=s["session"].get("lang", "en"),
                            conversation_id=s["conversation_id"], message_id=s["inbound"].message_id,
                            summary_text=s["text"], answerer=getattr(runtime, "answerer", None), triage=tri,
                            received_at=s["inbound"].received_at, memory=getattr(runtime, "session_context", None))

    def _triage(s: TurnState, tasks: list[Task]) -> Triage:
        """Triage this turn. Tasks parked behind verification keep the triage of the message that
        asked for them (this turn's text is then just the OTP)."""
        t = triage(s.get("text", ""), tasks, s.get("handoff_intent"))
        parked = s["session"].pop("pending_triage", None)
        if parked:
            t.hardship = t.hardship or parked["hardship"]
            if t.sentiment == "neutral":
                t.sentiment = parked["sentiment"]
            if t.hardship and "hardship" not in t.reasons:
                t.reasons.append("hardship")
            if t.sentiment in ("distressed", "frustrated") and t.category == "compliance":
                t.priority = "high"
        return t

    def _triage_event(t: Triage) -> dict:
        return {"type": EventType.TRIAGE.value, "data": t.as_dict()}

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
                or ((sess.get("pending_change") or sess.get("pending_action")) and (YES_RE.search(text) or NO_RE.search(text)))
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
        if sess.get("pending_action"):
            if YES_RE.search(s["text"]) or NO_RE.search(s["text"]):
                return "confirm_action"
            sess["pending_action"] = None        # they moved on: the question lapses, nothing is done
        if vs.stage in ("awaiting_account", "awaiting_otp", "locked"):
            return "verify"
        acc = extract_account_number(s["text"])
        scope = s["gw"].scope
        if acc and (scope is None or acc != scope.account_id):
            return "verify"
        return "plan"

    def handoff_node(s: TurnState):
        task = Task(intent=s["handoff_intent"], confidence=1.0)
        tri = _triage(s, [task])
        out = handlers.run_task(_ctx(s, tri), task)
        ev = {"type": EventType.HANDOFF.value, "data": {"intent": task.intent.value}}
        return {"tasks": [task], "replies": out.replies, "tickets": out.tickets, "events": [ev, _triage_event(tri)],
                "resolutions": [out.resolution], "triage": tri.as_dict()}

    def confirm_change_node(s: TurnState):
        sess = s["session"]
        pending = PendingChange(**sess["pending_change"])
        yes = bool(YES_RE.search(s["text"]))
        out = handlers.confirm_change(_ctx(s), pending, yes)
        sess["pending_change"] = None
        intent = Intent(pending.request.get("type", "waiver") + "_request")
        return {"tasks": [Task(intent=intent)], "replies": out.replies, "resolutions": [out.resolution]}

    def confirm_action_node(s: TurnState):
        """YES / NO to a question the agent asked: submit a waiver/settlement, escalate a payment."""
        sess = s["session"]
        action = sess.pop("pending_action")
        yes = bool(YES_RE.search(s["text"]))
        kind = action["kind"]
        intent = Intent(action["request"].get("type", "waiver") + "_request")
        tri = _triage(s, [Task(intent=intent)])
        ctx = _ctx(s, tri)
        try:
            out = handlers.submit_case(ctx, action) if yes else \
                handlers.HandlerOut([render("case_cancelled", ctx.lang)], resolution="auto")
        except NotVerified:
            return {"tasks": [Task(intent=intent)], "replies": [render("ask_account", ctx.lang)],
                    "resolutions": ["pending_verification"]}
        ev = {"type": EventType.TRIAGE.value, "data": {**tri.as_dict(), "confirmed": kind, "answer": "yes" if yes else "no"}}
        return {"tasks": [Task(intent=intent)], "replies": out.replies, "tickets": out.tickets,
                "resolutions": [out.resolution], "events": [ev], "triage": tri.as_dict()}

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
        sess["pending_triage"] = triage(s.get("text", ""), s["tasks"], s.get("handoff_intent")).as_dict()
        vs = VerificationState(**sess["verification"])
        vs.stage = "awaiting_account"
        sess["verification"] = vs.model_dump(mode="json")
        return {"replies": [render("ask_account", sess.get("lang", "en"))],
                "events": [{"type": EventType.VERIFICATION_STARTED.value, "data": {"stage": "ask_account"}}],
                "resolutions": ["pending_verification"]}

    def execute_node(s: TurnState):
        sess = s["session"]
        tri = _triage(s, s["tasks"])
        ctx = _ctx(s, tri)
        replies, tickets, resolutions, followups = [], [], [], []
        # Payment claims first, so a waiver / settlement sent with a screenshot sees what was paid.
        ordered = sorted(s["tasks"], key=lambda t: 0 if t.intent == Intent.PAYMENT_CLAIM else 1)
        for task in ordered:
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
            if out.pending_action:
                sess["pending_action"] = out.pending_action
            followups += out.followups
        sess["clarify_count"] = 0
        return {"replies": replies, "tickets": tickets, "resolutions": resolutions,
                "events": [_triage_event(tri)], "triage": tri.as_dict(), "followups": followups}

    def output_guard_node(s: TurnState):
        gw: PolicyGateway = s["gw"]
        verified_account = gw.scope.account_id if gw.scope else None
        constants = {settings.helpline}
        safe, events = [], []
        writer = getattr(runtime, "reply_writer", None)
        language = (s.get("translation") or {}).get("language") or s["session"].get("lang", "en")
        memory = getattr(runtime, "session_context", None)
        context = memory.window(session_key(s["conversation_id"], gw.scope)) if (writer and memory and gw.scope) else []
        guard = lambda text: check(text, verified_account=verified_account, allowed_numbers=gw.allowed_numbers,
                                   allowed_strings=gw.allowed_strings, constants=constants)
        for r in s.get("replies", []):
            if writer and r.template_id in REWRITABLE:
                new = writer.rewrite(r.text, language=language, context=context)
                if new and guard(new).ok:
                    events.append({"type": EventType.REPLY_REWRITTEN.value,
                                   "data": {"template_id": r.template_id, "language": language}})
                    r = r.model_copy(update={"text": new})
            g = guard(r.text)
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
                     ("confirm_action", confirm_action_node),
                     ("verify", verify_node), ("plan", plan_node), ("clarify", clarify_node),
                     ("start_verification", start_verification_node), ("execute", execute_node),
                     ("output_guard", output_guard_node)]:
        g.add_node(name, wrap_node(name, fn) if settings.tracing else fn)
    g.set_entry_point("screen")
    g.add_edge("screen", "normalize")
    g.add_conditional_edges("normalize", route_after_screen,
                            {k: k for k in ["handoff", "confirm_change", "confirm_action", "verify", "plan"]})
    g.add_conditional_edges("verify", route_after_verify, {"execute": "execute", "output_guard": "output_guard"})
    g.add_conditional_edges("plan", route_after_plan,
                            {k: k for k in ["clarify", "start_verification", "execute"]})
    for n in ["handoff", "confirm_change", "confirm_action", "clarify", "start_verification", "execute"]:
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
        self.trace_sink = None              # build_runtime sets it when SA_TRACING=true
        self.claims = claims
        self.normalizer = None              # set by build_runtime when SA_NORMALIZER=sarvam
        self.answerer = None                # SarvamAnswerer for free-form loan questions (same switch)
        self.reply_writer = None            # ReplyWriter: model re-phrases approved replies (same switch)
        from servicing_agent.platform.session_context import SessionContextStore
        self.session_context = SessionContextStore()   # build_runtime swaps in Redis on the live channel
        self.evidence = None                # EvidenceRecorder: full media/extraction/translation rows
        self.graph = build_graph(self)

    def _flush_trace(self, trace, msg: InboundMessage, result, error: str | None) -> None:
        """Write this turn's spans. Never blocks or fails the reply (same rule as the audit log)."""
        if trace is None:
            return
        try:
            media = [ev.kind for ev in msg.all_media()]
            inp = {"message_id": msg.message_id, "type": msg.type, "text": mask_text(msg.text)[:300] if msg.text else None,
                   "media_kinds": media, "batched_messages": len(msg.batched_message_ids) or 1}
            out = ({"resolution": result.resolution, "intents": [getattr(i, "value", i) for i in result.intents],
                    "verified": result.verified, "replies": len(result.replies), "tickets": len(result.tickets)}
                   if result is not None else {})
            self.trace_sink(trace.spans + [trace.turn_span("failed" if error else "succeeded", inp, out, error)])
        except Exception:  # noqa: BLE001 - tracing must not break the conversation
            logging.getLogger(__name__).exception("trace write failed for %s", msg.message_id)

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
            expired = False
            if scope and not verification.scope_valid(scope, now):
                if self.session_context:                    # the session ended: its context window goes too
                    self.session_context.forget(session_key(cid, scope))
                scope, sess["scope"] = None, None
                vs = VerificationState(**sess["verification"])
                vs.stage = "none"
                sess["verification"] = vs.model_dump(mode="json")
                expired = True
            elif scope:
                scope = scope.model_copy(update={"expires_at": now + verification.SESSION_TTL})
                sess["scope"] = scope.model_dump(mode="json")

            for ev in msg.all_media():
                if ev.kind == "voice_note" and ev.transcript and self.session_context:
                    said = mask_text(screen(ev.transcript).cleaned_text)[:200]
                    self.session_context.remember(session_key(cid, scope), "voice", {"said": said, "language": ev.language})
            gw = PolicyGateway(self.tools, self.cases, scope, cid, self.settings.settlement_window_hours,
                               claims=self.claims, message_id=msg.message_id)
            inbound_ev = {"type": EventType.INBOUND.value,
                          # Masked: audit_events is the broadly readable log (OTPs, UTRs, mobiles).
                          # Exact words live in the access-restricted `extractions` table.
                          "data": {"message_id": msg.message_id, "type": msg.type, "text": mask_text(msg.text),
                                   "verified": scope is not None}}
            trace = (TurnTrace(trace_id_for(cid, msg.message_id), cid, mask_text)
                     if self.settings.tracing and self.trace_sink else None)
            init = {"conversation_id": cid, "inbound": msg, "session": sess, "gw": gw,
                    "replies": [], "tickets": [], "events": [inbound_ev], "resolutions": [], "followups": []}
            if trace:
                init["trace"] = trace
            try:
                state = self.graph.invoke(init, config=trace_config(cid, msg, scope is not None, self.settings.planner))
            except Exception as exc:
                self._flush_trace(trace, msg, None, f"{type(exc).__name__}: {exc}"[:500])
                raise

            sess["scope"] = gw.scope.model_dump(mode="json") if gw.scope else None
            replies = state["final_replies"]
            normalized = normalized_view(msg, state, verified_before=scope is not None,
                                         verified_after=gw.scope is not None)
            norm_ev = {"type": EventType.TURN_NORMALIZED.value, "data": normalized}
            events = state["events"][:1] + [norm_ev] + gw.events + state["events"][1:]
            if expired:
                events.insert(1, {"type": EventType.SESSION_EXPIRED.value, "data": {}})
            for r in replies:
                events.append({"type": EventType.OUTBOUND.value,
                               "data": {"template_id": r.template_id, "text": mask_text(r.text),
                                        "verified": gw.scope is not None}})
            stamp = ist_stamp()
            for e in events:
                e.setdefault("at", stamp)
                if trace:
                    e.setdefault("trace_id", trace.trace_id)
            convo["events"] += events
            convo["tickets"] += state.get("tickets", [])
            b_entry = {"role": "borrower", "message_id": msg.message_id, "type": msg.type,
                       "text": msg.text, "at": stamp}
            if msg.media_id or msg.type in ("voice", "image"):
                # Carry the media so the Conversations screen can play the voice note / show the screenshot.
                b_entry["media_id"] = msg.media_id
                b_entry["media_kind"] = (msg.media.kind if msg.media else None) or msg.type
                cap = (msg.media.transcript if msg.media else None) or (msg.media.ocr_text if msg.media else None)
                if cap:
                    b_entry["media_text"] = mask_text(cap)
            convo["transcript"].append(b_entry)
            convo["transcript"] += [{"role": "agent", "text": r.text, "template_id": r.template_id, "at": stamp}
                                    for r in replies]
            self._attach_context(convo, gw, state.get("tickets", []), state.get("triage"), events)
            if self.audit_sink:
                self.audit_sink(cid, events)
            self._record_evidence(msg, state)

            res = state.get("resolutions", [])
            resolution = ("routed" if "routed" in res else "pending_verification" if "pending_verification" in res
                          else "clarify" if "clarify" in res else "auto" if res else "open")
            result = TurnResult(conversation_id=cid, replies=replies, verified=gw.scope is not None,
                                intents=[t.intent for t in state.get("tasks", [])], tickets=state.get("tickets", []),
                                events=events, resolution=resolution, normalized=normalized,
                                followups=state.get("followups", []))
            self._flush_trace(trace, msg, result, None)
            dump = result.model_dump(mode="json")
            for mid in msg.batched_message_ids or [msg.message_id]:
                processed[mid] = dump
            while len(processed) > self.DEDUPE_WINDOW:
                processed.pop(next(iter(processed)))
            return result


    def hand_verification_to_menu(self, conversation_id: str) -> None:
        """The WhatsApp menu flow (loan chooser + emailed code) takes over verification: drop the
        agent's own "share your account number" state so the next message isn't read as an account."""
        with self.conversations.lock(conversation_id):
            sess = self.conversations.load(conversation_id)["session"]
            vs = VerificationState(**sess["verification"])
            vs.stage = "none"
            sess["verification"] = vs.model_dump(mode="json")
            sess["pending_tasks"] = []

    def _attach_context(self, convo: dict, gw: PolicyGateway, tickets: list[dict], triage: dict | None,
                        events: list[dict]) -> None:
        """Carry the context forward: every ticket raised this turn gets the context bag (policy clauses
        applied, triage, recent conversation), and the clauses are written to the audit log."""
        if gw.policy_refs:
            ev = {"type": EventType.POLICY_APPLIED.value,
                  "data": {"clauses": list(gw.policy_refs), "version": policy().version},
                  "at": ist_stamp()}
            events.append(ev)
            convo["events"].append(ev)
        if not tickets:
            return
        bag = build_context_bag(convo, gw, triage)
        if self.session_context and gw.scope:
            bag["conversation_context"] = self.session_context.window(session_key(convo["conversation_id"], gw.scope))
        store = getattr(gw, "tools", None) and getattr(gw.tools, "tickets", None)
        for t in tickets:
            t.setdefault("summary", {})["context_bag"] = bag
            # The ticket row was persisted at create time (before the bag existed); write it back so the
            # approver context pack (built later from the DB) sees the policy clauses and triage.
            tid = t.get("ticket_id")
            if tid and store is not None and hasattr(store, "attach_context"):
                try:
                    store.attach_context(tid, bag)
                except Exception:  # noqa: BLE001 - never fail a turn on an audit write
                    log.exception("attach_context failed for %s", tid)

    def recheck_claim(self, conversation_id: str, slots: dict, account_id: str | None = None,
                      customer_id: str | None = None) -> TurnResult | None:
        """Second look at a recent unmatched payment (scheduled by the worker a couple of minutes
        after the "we'll check again" reply). Matched now -> confirmation + receipt; still not
        matched -> escalation ticket. Runs for the loan that was verified when the claim was made."""
        with self.conversations.lock(conversation_id):
            convo = self.conversations.load(conversation_id)
            sess = convo["session"]
            if sess.get("scope") and (account_id is None or sess["scope"]["account_id"] == account_id):
                scope = AuthScope(**sess["scope"])
            elif account_id and customer_id:
                # System recheck for the loan the claim was made on (the in-memory session may be
                # gone after a restart). Only the claim tools run; nothing else is read.
                now = datetime.utcnow()
                scope = AuthScope(account_id=account_id, customer_id=customer_id, contact_id="system-recheck",
                                  method="account+mobile", verified_at=now, expires_at=now + timedelta(minutes=5))
            else:
                return None
            mid = f"recheck_{slots.get('media_id') or slots.get('utr') or uuid.uuid4().hex[:8]}"
            gw = PolicyGateway(self.tools, self.cases, scope, conversation_id, self.settings.settlement_window_hours,
                               claims=self.claims, message_id=mid)
            ctx = handlers.Ctx(gw=gw, settings=self.settings, lang=sess.get("lang", "en"),
                               conversation_id=conversation_id, message_id=mid,
                               summary_text="(automatic recheck of a payment claim)", received_at=datetime.utcnow(),
                               memory=self.session_context if scope.contact_id != "system-recheck" else None)
            task = Task(intent=Intent.PAYMENT_CLAIM, confidence=1.0, slots={**slots, "recheck": True})
            out = handlers.run_task(ctx, task)
            replies, events = [], [{"type": EventType.TOOL_CALL.value, "data": {"tool": "claims.recheck"}}]
            for r in out.replies:
                g = check(r.text, verified_account=scope.account_id, allowed_numbers=gw.allowed_numbers,
                          allowed_strings=gw.allowed_strings, constants={self.settings.helpline})
                if g.ok:
                    replies.append(r)
                else:
                    events.append({"type": EventType.OUTPUT_BLOCKED.value,
                                   "data": {"reason": g.reason, "detail": g.detail, "template_id": r.template_id}})
                    replies.append(render("safe_fallback", sess.get("lang", "en")))
            events = events + gw.events + [{"type": EventType.OUTBOUND.value,
                                            "data": {"template_id": r.template_id, "text": mask_text(r.text),
                                                     "verified": True}} for r in replies]
            stamp = ist_stamp()
            for e in events:
                e.setdefault("at", stamp)
            convo["events"] += events
            convo["tickets"] += out.tickets
            convo["transcript"] += [{"role": "agent", "text": r.text, "template_id": r.template_id, "at": stamp}
                                    for r in replies]
            self._attach_context(convo, gw, out.tickets, ctx.triage_info() or None, events)
            if self.audit_sink:
                self.audit_sink(conversation_id, events)
            return TurnResult(conversation_id=conversation_id, replies=replies, verified=True,
                              intents=[Intent.PAYMENT_CLAIM], tickets=out.tickets, events=events,
                              resolution="routed" if out.tickets else "open" if out.followups else "auto",
                              followups=out.followups)


_UTR_IN_TEXT = re.compile(r"(?<!\d)\d{12}(?!\d)")
_MOBILE_IN_TEXT = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
_OTP_IN_TEXT = re.compile(r"(?<!\d)\d{6}(?!\d)")


def build_context_bag(convo: dict, gw: PolicyGateway, triage: dict | None, last_messages: int = 8) -> dict:
    """What travels with an escalation or approval: the policy applied and why, in one place."""
    p = policy()
    return {
        "policy_version": p.version,
        "policy_clauses": [p.get(c).ref() for c in gw.policy_refs],
        # The decision record: which clause decided, on which facts, with what action.
        "decisions": list(gw.decisions),
        "reviewer_note": "; ".join(f"{d['clause']} ({d['title']}) -> {d['action'].get('action')} on "
                                   + ", ".join(f"{k}={v}" for k, v in d["facts"].items()) for d in gw.decisions),
        "triage": triage,
        "verified_account": gw.scope.account_id if gw.scope else None,
        "recent_conversation": [{"role": m["role"], "text": mask_text(m.get("text")), "at": m.get("at")}
                                for m in convo.get("transcript", [])[-last_messages:]],
    }


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
        "triage": state.get("triage"),
        "policy_clauses": list(state["gw"].policy_refs) if state.get("gw") else [],
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
