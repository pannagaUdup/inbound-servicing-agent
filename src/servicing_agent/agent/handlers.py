"""Intent handlers: code, not prompts. Each one calls tools only through the gateway."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from servicing_agent.agent.gateway import PolicyGateway
from servicing_agent.agent.templates import STATUS_TEXT, TEAM_NAMES, money, render
from servicing_agent.config import Settings
from servicing_agent.contracts.cases import CaseRequest
from servicing_agent.contracts.events import EventType
from servicing_agent.contracts.state import HANDOFF_INTENTS, Intent, PendingChange, Reply, Task
from servicing_agent.agent.tools.reconciliation import FLAGGED, Verdict, to_india
from servicing_agent.platform.cases import is_material_change

log = logging.getLogger(__name__)

Resolution = Literal["auto", "routed", "clarify", "pending"]


@dataclass
class HandlerOut:
    replies: list[Reply] = field(default_factory=list)
    tickets: list[dict] = field(default_factory=list)
    resolution: Resolution = "auto"
    pending_change: PendingChange | None = None
    followups: list[dict] = field(default_factory=list)   # e.g. recheck an unmatched claim in 2 minutes
    pending_action: dict | None = None      # asks the borrower to confirm (YES/NO) before acting


@dataclass
class Ctx:
    gw: PolicyGateway
    settings: Settings
    lang: str
    conversation_id: str
    message_id: str
    summary_text: str
    answerer: object | None = None          # SarvamAnswerer for free-form loan questions (optional)
    triage: object | None = None            # Triage for this turn (compliance category, sentiment)
    received_at: datetime | None = None     # when the borrower sent the message (UTC)
    # Payment claims decided earlier in this turn ({"status": matched|checking|unverified|escalated, "amount"}),
    # so a waiver / settlement sent with a screenshot can look at what was actually paid.
    claims_this_turn: list = field(default_factory=list)
    memory: object | None = None            # SessionContextStore: this verified session's context window
    reminder: dict | None = None            # the payment reminder this conversation got (loan, amount, sent_at)

    @property
    def session(self) -> str | None:
        from servicing_agent.platform.session_context import session_key
        return session_key(self.conversation_id, self.gw.scope)

    def cite(self, *clause_ids: str):
        self.gw.cite(*clause_ids)

    def decision(self, d) -> None:
        """Record a policy decision on the turn (gateway -> ticket decision record) and in the context log."""
        from servicing_agent.agent.policy import policy
        self.gw.decisions.append(d.record(policy().version))
        if self.memory:
            self.memory.remember(self.session, "decision", {"clause": d.clause.id, "action": d.action.get("action")})

    def triage_info(self) -> dict:
        return self.triage.as_dict() if self.triage is not None else {}

    @property
    def hardship(self) -> bool:
        return bool(self.triage is not None and self.triage.hardship)


def _route(ctx: Ctx, request_type: str, task: Task, handoff: bool = False) -> HandlerOut:
    summary = {"intent": task.intent.value, "borrower_message": ctx.summary_text[:500],
               "language": ctx.lang, "verified": ctx.gw.scope is not None, "slots": task.slots,
               "triage": ctx.triage_info()}
    t = ctx.gw.ticket(request_type, summary)
    tpl = "handoff" if handoff else "routed"
    reply = render(tpl, ctx.lang, ticket_id=t["ticket_id"], helpline=ctx.settings.helpline,
                   team=TEAM_NAMES.get(t["queue"], "servicing"))
    return HandlerOut([reply], [t], "routed")


def _sim_swap_review(ctx: Ctx, what: str, **extra) -> dict | None:
    """SEC-01: the verified mobile was added recently. Returns the (open or new) security ticket, or None."""
    if not ctx.gw.recent_contact_change():
        return None
    ctx.cite("SEC-01")
    t = ctx.gw.open_ticket("sim_swap_review")            # one open review covers later requests
    if t is None:
        t = ctx.gw.ticket("sim_swap_review", {"reason": "recent_contact_change", "requested": what,
                                              "contact_id": ctx.gw.scope.contact_id, "triage": ctx.triage_info(),
                                              **extra})
        t["_new"] = True
    return t


def _sim_swap_hold(ctx: Ctx, what: str) -> HandlerOut | None:
    """Documents wait for the security team's check when the number is new on the loan."""
    t = _sim_swap_review(ctx, what)
    if t is None:
        return None
    return HandlerOut([render("sim_swap_hold", ctx.lang, what=what, ticket_id=t["ticket_id"])],
                      [t] if t.pop("_new", False) else [], "routed")


def handle_foreclosure(ctx: Ctx, task: Task) -> HandlerOut:
    held = _sim_swap_hold(ctx, "closure quote")
    if held:
        return held
    q = ctx.gw.foreclosure_quote()
    b = q["breakup"]
    return HandlerOut([render("foreclosure_quote", ctx.lang, amount=money(q["amount"]), valid_until=q["valid_until"],
                              principal=money(b["principal"]), overdue=money(b["overdue"]),
                              charges=money(b["charges"]), foreclosure_fee=money(b["foreclosure_fee"]))])


def handle_emi_due(ctx: Ctx, task: Task) -> HandlerOut:
    d = ctx.gw.dues()
    return HandlerOut([render("emi_due", ctx.lang, emi_amount=money(d["emi_amount"]),
                              next_due_date=d["next_due_date"], overdue_amount=money(d["overdue_amount"]))])


def handle_escalation_request(ctx: Ctx, task: Task) -> HandlerOut:
    """The borrower asks to escalate (policy ESC-02). Already escalated and still open: the same reference.
    Otherwise ask first (YES / NO); nothing is raised until they confirm (see raise_escalation)."""
    ctx.cite("ESC-02")
    prior = ctx.gw.open_ticket("borrower_escalation")          # already escalated and still open: same reference
    if prior:
        return HandlerOut([render("escalation_existing", ctx.lang, ticket_id=prior["ticket_id"])], resolution="routed")
    row = ctx.gw.latest_claim(minutes=24 * 60)
    silent = bool(row and row.get("result") in {v.value for v in FLAGGED})
    detail = _claim_detail({"amount": row.get("amount"), "utr": row.get("utr")}) if (row and not silent) else ""
    return HandlerOut([render("escalation_confirm", ctx.lang, about=f" about your payment{detail}" if row else "",
                              buttons=["Yes", "No"])], resolution="pending",
                      pending_action={"kind": "escalate", "request": {"type": "escalation"},
                                      "borrower_text": ctx.summary_text[:500], "triage": ctx.triage_info()})


def raise_escalation(ctx: Ctx, action: dict) -> HandlerOut:
    """The borrower said YES: a compliance_review ticket, linked to their latest payment claim if there is one,
    with the reference in the reply. If that claim was escalated silently (CLM-03), the reply doesn't reveal it."""
    ctx.cite("ESC-02", "ESC-01")
    prior = ctx.gw.open_ticket("borrower_escalation")          # raised meanwhile (e.g. a double YES)
    if prior:
        return HandlerOut([render("escalation_existing", ctx.lang, ticket_id=prior["ticket_id"])], resolution="routed")
    last = None
    row = ctx.gw.latest_claim(minutes=24 * 60)                  # this loan's latest claim, any session
    if row:
        last = {"utr": row.get("utr"), "amount": row.get("amount"), "last_result": row.get("result"),
                "ticket_id": row.get("ticket_id")}
    silent = bool(last and last.get("last_result") in {v.value for v in FLAGGED})
    summary = {"intent": "escalation_request", "requested_by_borrower": True, "borrower_confirmed": True,
               "borrower_text": action.get("borrower_text") or ctx.summary_text[:500],
               "payment": ({"utr_last4": (last.get("utr") or "")[-4:], "amount": last.get("amount"),
                            "result": last.get("last_result"), "linked_ticket": last.get("ticket_id")} if last else None),
               "earlier_review_silent": silent, "triage": action.get("triage") or ctx.triage_info()}
    t = ctx.gw.ticket("borrower_escalation", summary)
    if ctx.memory:
        ctx.memory.remember(ctx.session, "escalation", {"ticket_id": t["ticket_id"]})
    linked = ""
    if last and last.get("ticket_id") and not silent:
        ctx.gw.allow({"ticket_id": last["ticket_id"]})
        linked = f" It's linked to your payment review {last['ticket_id']}."
    detail = _claim_detail({"amount": last.get("amount"), "utr": last.get("utr")}) if last else ""
    if last:
        if last.get("amount") is not None:
            ctx.gw.allow(float(last["amount"]))
        if last.get("utr"):
            ctx.gw.allow(last["utr"][-4:])
    return HandlerOut([render("escalation_raised", ctx.lang, ticket_id=t["ticket_id"], linked=linked,
                              about=f" about your payment{detail}" if last else "")], [t], "routed")


def handle_payment_difficulty(ctx: Ctx, task: Task) -> HandlerOut:
    """Can't pay in full: acknowledge, then offer the options (policy HRD-02). Nothing is raised until the
    borrower picks one; a waiver or settlement then goes through its own confirm-first flow.

    With an amount ("I can only pay 5000"): send a payment link for that amount, show what's left, and offer to
    ask for the rest to be waived or settled (the request defaults to that shortfall)."""
    from servicing_agent.agent.planner import extract_amount
    if ctx.settings.hardship_ladder:
        amount = task.slots.get("amount") or extract_amount(ctx.summary_text)
        return hardship_ladder(ctx, hardship_stance(ctx.summary_text, task.intent, amount), amount)
    d = ctx.gw.dues(Intent.EMI_DUE_QUERY)
    due = float(d["overdue_amount"] or 0) or float(d["emi_amount"] or 0)
    ctx.cite("HRD-02", "HRD-01")
    amount = task.slots.get("amount") or extract_amount(ctx.summary_text)
    if amount and 0 < float(amount) < due:
        amount = float(amount)
        shortfall = round(due - amount, 2)
        ctx.gw.allow([amount, due, shortfall])
        link = ctx.gw.payment_link(amount)
        if ctx.memory:
            ctx.memory.remember(ctx.session, "part_payment", {"amount": amount, "due": due, "shortfall": shortfall})
        return HandlerOut([render("payment_difficulty_partial", ctx.lang, amount=money(amount), due=money(due),
                                  shortfall=money(shortfall), payment_link=link["payment_link"])], resolution="pending")
    if amount and float(amount) >= due:
        return handle_pay_now(ctx, Task(intent=Intent.PAY_NOW, confidence=1.0, slots={"amount": float(amount)}))
    return HandlerOut([render("payment_difficulty", ctx.lang, due=money(due))], resolution="pending")


# ---- hardship ladder (policy HRD-03, WVR-03/04/05, STL-03..07): waivers and settlements are a last resort
HARDSHIP_TTL = 7 * 24 * 3600
_REFUSE_ALL = re.compile(
    r"\b(no (state|position|way)\b[^.?!]{0,20}\bpay)|\b(can'?t|cannot|won'?t|unable to|not able to)\b[^.?!]{0,25}\bpay\w*\b"
    r"[^.?!]{0,20}\b(at all|anything|any ?thing|any amount|nothing|ever)\b|\b(final|full and final|one.?time) settlement\b", re.I)
_INSTALMENTS = re.compile(r"\b(instal+ments?|in (parts|pieces|tranches)|tranches?|part by part|monthly|split (it|the)|"
                          r"pay (it )?in \d+|over \d+ months|emis? for (the|this) settlement)\b", re.I)


def hardship_stance(text: str, intent: Intent | None = None, amount: float | None = None) -> str:
    """What the borrower is asking for, from their words (no model needed for these)."""
    if _INSTALMENTS.search(text or ""):
        return "ask_instalments"
    if _REFUSE_ALL.search(text or ""):
        return "refuse_all"
    if intent == Intent.SETTLEMENT_REQUEST or re.search(r"\bsettle", text or "", re.I):
        return "ask_settlement"
    if intent == Intent.WAIVER_REQUEST or re.search(r"\bwaive", text or "", re.I):
        return "ask_waiver"
    if amount:
        return "pay_part"
    return "unable_now"


def _ladder_key(ctx: Ctx) -> str:
    return f"hardship:{ctx.gw.scope.account_id}"


def _ladder_state(ctx: Ctx) -> dict:
    return ctx.memory.get_state(_ladder_key(ctx)) if ctx.memory else {}


def reset_ladder(memory, account_id: str) -> None:
    """Start the ladder again for this loan (the borrower started over with 'hi' / the menu)."""
    if memory and account_id:
        memory.set_state(f"hardship:{account_id}", {}, 1)


def _save_ladder(ctx: Ctx, state: dict) -> None:
    if ctx.memory:
        ctx.memory.set_state(_ladder_key(ctx), state, HARDSHIP_TTL)


def _ladder_facts(ctx: Ctx, stance: str, state: dict) -> tuple[dict, dict, dict | None]:
    """(facts for 'hardship', loan facts, pre-approval action). Waiver eligibility and settlement pre-approval are
    themselves decided by the policy document (situations waiver_eligibility, settlement_preapproval)."""
    from servicing_agent.agent.planner import extract_reason
    from servicing_agent.agent.policy import policy
    f = ctx.gw.hardship_facts()
    overdue = f["overdue"] or f["emi"]
    reason = extract_reason(ctx.summary_text) or state.get("reason")
    state["reason"] = reason
    base = {"nudges_given": int(state.get("nudges", 0)),
            "paid_share": round(f["paid_30d"] / overdue, 3) if overdue > 0 else 1.0,
            "dpd_band": f.get("dpd_band") or "", "reason_known": bool(reason and reason != "other")}
    waiver = policy().decide("waiver_eligibility", base)
    pre = policy().decide("settlement_preapproval", {"preapproved_flag": f.get("preapproved_flag", False),
                                                     "recovery": f.get("recovery", 0.0), "dpd_band": base["dpd_band"]})
    has_deal = f.get("charges", 0) > 0 and overdue - f.get("charges", 0) > 0     # charges to waive, something to pay
    facts = {"nudges_given": base["nudges_given"], "stance": stance, "waiver_eligible": bool(waiver) and has_deal,
             "waiver_offered": state.get("offered") == "waiver", "waiver_declines": int(state.get("waiver_declines", 0)),
             "preapproved": bool(pre), "settlement_offered": state.get("offered") == "settlement",
             "waiver_declined": state.get("offered") == "waiver_declined"}
    for d in (waiver, pre):
        if d:
            ctx.cite(d.clause.id)
    return facts, {**f, "overdue": overdue, "paid_share": base["paid_share"]}, (pre.action if pre else None)


def hardship_ladder(ctx: Ctx, stance: str, amount: float | None = None, requested: str | None = None) -> HandlerOut:
    """One step of the ladder. Code works out the facts; the policy document (decides: hardship) picks the step."""
    from servicing_agent.agent.policy import policy
    state = _ladder_state(ctx)
    f0 = ctx.gw.hardship_facts()
    if state.get("dpd_band") and state["dpd_band"] != f0.get("dpd_band"):
        # The loan moved to another days-past-due band: rounds counted under the old facts don't apply.
        state = {"reason": state.get("reason")}
    state["dpd_band"] = f0.get("dpd_band")
    overdue0 = f0["overdue"] or f0["emi"]
    due_line = f" and your next due date is {f0['next_due_date']}" if f0.get("next_due_date") else ""
    if stance == "pay_part" and amount and 0 < amount < overdue0:
        # They offered a part payment: take it right away (link), no waiver / settlement talk.
        shortfall = round(overdue0 - amount, 2)
        ctx.gw.allow([amount, overdue0, shortfall])
        link = ctx.gw.payment_link(amount)
        ctx.cite("HRD-03")
        return HandlerOut([render("hardship_partial", ctx.lang, amount=money(amount), shortfall=money(shortfall),
                                  due_line=due_line, payment_link=link["payment_link"])], resolution="pending")
    if stance == "pay_part" and amount and amount >= overdue0:
        return handle_pay_now(ctx, Task(intent=Intent.PAY_NOW, confidence=1.0, slots={"amount": float(amount)}))
    # A waiver was offered and they still say they can't / want something else: that's a decline.
    if state.get("offered") == "waiver" and stance != "ask_waiver":
        state["waiver_declines"] = int(state.get("waiver_declines", 0)) + 1
    facts, f, pre = _ladder_facts(ctx, stance, state)
    decision = policy().decide("hardship", facts)
    act = decision.action
    ctx.cite(decision.clause.id, *(act.get("cite") or []))
    ctx.decision(decision)
    replies, tickets = [], []
    pending = None
    if act["action"] == "offer_waiver":
        # Pay the overdue amount minus the charges now; the charges go up for waiver (WVR-05). YES / NO.
        charges = round(f["charges"], 2)
        pay = round(f["overdue"] - charges, 2)
        again = state.get("offered") == "waiver"
        state.update(offered="waiver", pay=pay, charges=charges)
        ctx.gw.allow([pay, charges, f["overdue"]])
        replies.append(render("hardship_offer_cond_waiver_again" if again else "hardship_offer_cond_waiver", ctx.lang,
                              pay=money(pay), charges=money(charges), overdue=money(f["overdue"])))
        pending = {"kind": "conditional_waiver", "request": {"type": "waiver"}, "pay": pay, "charges": charges}
    elif act["action"] == "offer_preapproved_settlement":
        total = round(f.get("principal_outstanding", 0) + f["overdue"], 2)
        amt = round(total * float((pre or {}).get("amount_pct", 0.6)), 2)
        ctx.gw.allow([amt, total])
        link = ctx.gw.payment_link(amt)
        state.update(offered="settlement", settlement_amount=amt)
        if ctx.gw.open_ticket("preapproved_settlement") is None:     # one record per offer, however often it's re-sent
            tickets.append(ctx.gw.ticket("preapproved_settlement", {
                "offered_amount": amt, "total_outstanding": total, "one_time": True, "facts": facts,
                "policy": decision.record(policy().version), "triage": ctx.triage_info()}))
        replies.append(render("hardship_offer_preapproved", ctx.lang, amount=money(amt), total=money(total),
                              payment_link=link["payment_link"]))
    elif act["action"] == "route_collections":
        prior = ctx.gw.open_ticket("collections_review")
        t = prior or ctx.gw.ticket("collections_review", {
            "why": "instalments requested for the settlement" if stance == "ask_instalments" else
                   "can't pay; no waiver or pre-approved settlement available",
            "facts": facts, "reason": state.get("reason"), "borrower_text": ctx.summary_text[:500],
            "policy": decision.record(policy().version), "triage": ctx.triage_info()})
        if not prior:
            tickets.append(t)
        state["offered"] = "collections"
        why = None if stance == "ask_instalments" else _waiver_ineligible_why(ctx, f, facts, state)
        if why:
            replies.append(render("hardship_collections_why", ctx.lang, why=why, ticket_id=t["ticket_id"]))
        else:
            replies.append(render("hardship_collections", ctx.lang, ticket_id=t["ticket_id"]))
    else:
        state["nudges"] = facts["nudges_given"] + 1
        if requested:
            replies.append(render("hardship_not_yet", ctx.lang))
        variant = min(facts["nudges_given"], 2) + 1
        replies.append(render(f"hardship_encourage_{variant}", ctx.lang, overdue=money(f["overdue"]), due_line=due_line,
                              due_by=f" before {f['next_due_date']}" if f.get("next_due_date") else ""))
    _save_ladder(ctx, state)
    return HandlerOut(replies, tickets, "routed" if tickets else "pending", pending_action=pending)


def decline_waiver(ctx: Ctx, action: dict) -> HandlerOut:
    """NO to the waiver offer: dropped. No approval, and no settlement for this loan (WVR-06)."""
    state = _ladder_state(ctx)
    state.update(offered="waiver_declined")
    _save_ladder(ctx, state)
    f = ctx.gw.hardship_facts()
    ctx.cite("WVR-06")
    due_line = f", and your next due date is {f['next_due_date']}" if f.get("next_due_date") else ""
    return HandlerOut([render("waiver_declined_ack", ctx.lang, overdue=money(f["overdue"] or f["emi"]),
                              due_line=due_line)], resolution="auto")


def request_same_day_waiver(ctx: Ctx, action: dict) -> HandlerOut:
    """YES to the waiver offer (WVR-05): the waiver of the charges goes for approval now. Payment comes after
    approval: a same-day link for the overdue minus the charges (sent when the case is approved)."""
    pay, charges = float(action["pay"]), float(action["charges"])
    state = _ladder_state(ctx)
    if ctx.gw.open_case() is not None:
        case = ctx.gw.open_case()
        return HandlerOut([render("case_active", ctx.lang, desc=_desc(case.request.model_dump(mode="json")),
                                  case_id=case.case_id, stage=STATUS_TEXT.get(case.status.value, "under review"))],
                          resolution="routed")
    req = CaseRequest(type="waiver", amount=charges, components=["penal", "bounce"], plan="pay_same_day",
                      reason_category=state.get("reason") or "financial_difficulty",
                      borrower_text=f"Asked to waive the charges of ₹{money(charges)}; would pay ₹{money(pay)} the same "
                                    f"day it's approved")
    out = submit_case(ctx, {"request": req.model_dump(mode="json"), "tickets": ["approval"],
                            "triage": ctx.triage_info(), "policy_refs": ["WVR-03", "WVR-05", "APR-01", "APR-02"]})
    case = ctx.gw.open_case()
    state.update(offered="waiver_requested", pay=pay, charges=charges)
    _save_ladder(ctx, state)
    if case is None:
        return out
    ctx.gw.allow([pay, charges])
    return HandlerOut([render("cond_waiver_requested", ctx.lang, charges=money(charges), pay=money(pay),
                              case_id=case.case_id)], out.tickets, "routed")


def _waiver_thresholds() -> tuple[float | None, list[str]]:
    """From the policy document: the paid share a waiver needs, and the DPD bands it's open to (WVR-03/04)."""
    from servicing_agent.agent.policy import policy
    share, bands = None, []
    for c in policy().clauses:
        if c.decides != "waiver_eligibility":
            continue
        for alt in c.when:
            for fact, op, val in alt:
                if fact == "paid_share" and op in (">=", ">"):
                    share = float(val)
                if fact == "dpd_band" and op == "==":
                    bands.append(str(val))
                if fact == "dpd_band" and op == "in":
                    bands += [str(v) for v in val]
    return share, bands


def _waiver_ineligible_why(ctx: Ctx, f: dict, facts: dict, state: dict) -> str:
    """Plain words for why the waiver isn't available: the loan's own facts against the policy's thresholds."""
    if facts.get("waiver_declines", 0) >= 2:
        return "the waiver didn't work for you"
    share, bands = _waiver_thresholds()
    parts = []
    if not f.get("dpd"):
        return "nothing is overdue on it right now, so there are no late charges to waive"
    if bands and f.get("dpd_band") not in bands:
        lo, hi = min(int(b.split("-")[0]) for b in bands), max(int(b.split("-")[1]) for b in bands)
        ctx.gw.allow([f.get("dpd", 0), lo, hi])
        need = f"{hi} days or less" if lo == 0 else f"between {lo} and {hi} days"
        parts.append(f"it is {f.get('dpd', 0)} days past due (a waiver needs {need} past due)")
    if share is not None and f.get("paid_share", 0) < share:
        pct, need = int(round(f.get("paid_share", 0) * 100)), int(round(share * 100))
        ctx.gw.allow([pct, need])
        parts.append(f"{pct}% of the overdue amount has been paid in the last 30 days (at least {need}% is needed)")
    if not parts and f.get("charges", 0) <= 0:
        parts.append("there are no late-payment or bounce charges on it to waive")
    return " and ".join(parts) if parts else ""


def _ladder_allows(ctx: Ctx, kind: str, text: str) -> HandlerOut | None:
    """A waiver asked for directly: allowed only once the ladder offers it. A settlement is never raised as an
    approval case on the ladder: it's either the pre-approved one-time offer or the collections team."""
    if not ctx.settings.hardship_ladder or ctx.gw.scope is None:
        return None
    stance = hardship_stance(text, Intent(kind + "_request"))
    facts, _, _ = _ladder_facts(ctx, stance, dict(_ladder_state(ctx)))
    from servicing_agent.agent.policy import policy
    offers_waiver = policy().decide("hardship", facts).action["action"] == "offer_waiver"
    return hardship_ladder(ctx, stance, requested=None if (kind == "waiver" and offers_waiver) else kind)


# Topics a short follow-up ("tell me more") can refer to, and the query that finds their policy clauses.
TOPIC_QUERIES = {
    "payment_difficulty": "can't pay in full options part payment waiver settlement approval stages consequence credit",
    "waiver_request": "waiver approval stages consequence what happens",
    "settlement_request": "settlement approval stages consequence credit bureau what happens",
    "escalation_request": "escalate compliance team reference",
    "policy_question": None,
}
LADDER_TOPIC_QUERY = "can't pay encourage payment due date credit score late charges part payment"
_FOLLOWUP = re.compile(r"^\W*(ok(ay)?\W+)?(please\W+)?(tell me( more)?|explain( (it|that|this|more))?|more( details| info)?|"
                       r"details|elaborate|go on|what does (that|this|it) mean|how( does (that|this|it) work)?|"
                       r"like what|what are (the|my) options|which (one|options?)|and)(\W+please)?\W*$", re.I)


_REFUSAL = re.compile(r"^\W*(no|nope|not possible|i can'?t|i cannot|can'?t|cannot|impossible|no way|not now|"
                      r"i won'?t be able to|not able to|sorry,? (i )?can'?t)\b[^?]{0,60}$", re.I)


_CANT = re.compile(r"\b(can'?t|cannot|won'?t|unable|not able|not possible|impossible|no money|don'?t have)\b", re.I)


def is_refusal(text: str | None) -> bool:
    """After we encouraged a payment: a 'no / I can't', or any short reply saying they can't or why
    ('I lost my job, I can't'). Continues the ladder."""
    from servicing_agent.agent.planner import extract_reason
    if not text or len(text) > 160:
        return False
    return bool(_REFUSAL.match(text.strip()) or _CANT.search(text) or extract_reason(text))


def is_followup(text: str | None) -> bool:
    return bool(text and len(text) <= 40 and _FOLLOWUP.match(text.strip()))


def _explain_topic(ctx: Ctx, topic: str) -> HandlerOut:
    """'Tell me more' after an options / policy reply: the approved borrower lines for that topic."""
    from servicing_agent.agent.policy import policy
    ladder = ctx.settings.hardship_ladder
    query = (LADDER_TOPIC_QUERY if ladder and topic == "payment_difficulty" else TOPIC_QUERIES.get(topic)) or ctx.summary_text
    hits = [h for h in policy().search(query, with_borrower_line=True) if (h.bm25 > 0 or h.cosine > 0)
            # With the ladder on, can't-pay explanations are about paying (no waiver / settlement menu);
            # with it off, the ladder's own clauses aren't part of the explanation.
            and (h.clause.decides != "hardship" if not ladder else h.clause.id not in ("WVR-03", "HRD-07", "HRD-08"))][:4]
    clauses = [h.clause for h in hits]                      # most relevant first: the borrower's options lead
    ctx.gw._event(EventType.TOOL_CALL, tool="policy.search", mode="followup", topic=topic,
                  hits=[c.id for c in clauses])
    if not clauses:
        return HandlerOut([render("clarify", ctx.lang)], resolution="clarify")
    ctx.cite(*[c.id for c in clauses])
    bullets = "\n".join(f"• {c.borrower}" for c in clauses)
    nxt = {"payment_difficulty": "difficulty", "waiver_request": "request", "settlement_request": "request"}.get(topic, "")
    if ladder and topic == "payment_difficulty":
        nxt = "pay"
    return HandlerOut([render("policy_explain", ctx.lang, bullets=bullets,
                              next=render(f"policy_next_{nxt}", ctx.lang).text if nxt else "")])


def handle_pay_now(ctx: Ctx, task: Task) -> HandlerOut:
    d = ctx.gw.dues(Intent.PAY_NOW)
    overdue = float(d["overdue_amount"] or 0)
    emi = float(d["emi_amount"] or 0)
    requested = task.slots.get("amount")                       # "I can pay 5000" -> 5000
    wants_emi = bool(re.search(r"\bemi\b", (ctx.summary_text or "").lower()))
    if requested:
        amount = float(requested)                             # borrower named an amount -> honour it
    elif overdue > 0:
        amount = overdue                                      # the usual case: pay what's overdue
    elif wants_emi and emi > 0:
        amount = emi                                          # nothing overdue, but they explicitly want to pay the EMI
    else:
        # Nothing overdue and no explicit EMI/amount request -> don't push a link for ₹0.
        return HandlerOut([render("no_overdue_pay", ctx.lang, emi_amount=money(emi))])
    ctx.gw.allow(float(amount))
    link = ctx.gw.payment_link(amount)
    return HandlerOut([render("pay_now", ctx.lang, amount=money(amount), payment_link=link["payment_link"])])


_CHANNEL = {"UPI": "UPI", "NACH": "auto-debit", "NEFT": "bank transfer", "CASH": "cash"}
_STATUS = {"success": "received", "failed": "failed", "pending": "pending", "reversed": "reversed"}


def handle_payment_history(ctx: Ctx, task: Task) -> HandlerOut:
    h = ctx.gw.payment_history()
    pays = h["payments"]
    if not pays:
        return HandlerOut([render("payment_history_empty", ctx.lang, months=h["months"])])
    lines = "\n".join(f"• {p['date']}: ₹{money(p['amount'])} by {_CHANNEL.get(p['channel'], p['channel'])} "
                      f"({_STATUS.get(p['status'], p['status'])}), UTR {p['utr']}" for p in pays)
    return HandlerOut([render("payment_history", ctx.lang, count=len(pays), lines=lines)])


# A specific question gets that answer, not the whole summary.
_ASKS = [
    ("loan_rate", re.compile(r"interest|rate of interest|\broi\b|\brate\b", re.I)),
    ("loan_outstanding", re.compile(r"outstanding|balance|principal|(what|how much)('s| is)? (is )?(left|remaining|pending)", re.I)),
    ("loan_emis_left", re.compile(r"emis? (are |is )?(left|remaining|pending)|how many emis|\btenure\b", re.I)),
]


def _emis(n) -> str:
    return f"{n} EMI" if str(n) == "1" else f"{n} EMIs"


def handle_loan_summary(ctx: Ctx, task: Task) -> HandlerOut:
    s = ctx.gw.loan_summary()
    asked = [tpl for tpl, rx in _ASKS if rx.search(ctx.summary_text or "")]
    if asked and not re.search(r"summary|details|status", ctx.summary_text or "", re.I):
        vals = dict(product=str(s["product"]).replace("_", "-"), rate=f"{s['interest_rate']:.2f}",
                    principal=money(s["principal_outstanding"]), overdue=money(s["overdue_amount"]),
                    emi=money(s["emi_amount"]), emis_left=_emis(s["tenure_left"]), next_due_date=s["next_due_date"])
        text = " ".join(render(tpl, ctx.lang, **vals).text for tpl in asked)
        return HandlerOut([Reply(text=text, template_id="+".join(asked))])
    return HandlerOut([render("loan_summary", ctx.lang, product=str(s["product"]).replace("_", "-"),
                              principal=money(s["principal_outstanding"]), rate=f"{s['interest_rate']:.2f}",
                              emi=money(s["emi_amount"]), tenure_left=_emis(s["tenure_left"]),
                              next_due_date=s["next_due_date"], overdue=money(s["overdue_amount"]))])


def _facts_reply(ctx: Ctx, f: dict) -> Reply:
    return render("account_facts", ctx.lang, product=str(f.get("product", "")).replace("_", "-"),
                  sanction=money(f.get("sanction_amount")), principal=money(f.get("principal_outstanding")),
                  rate=f"{float(f.get('interest_rate') or 0):.2f}", emi=money(f.get("emi_amount")),
                  tenure_months=f.get("tenure_months", "-"), tenure_left=f.get("tenure_left", "-"),
                  next_due_date=f.get("next_due_date"), overdue=money(f.get("overdue_amount")),
                  helpline=ctx.settings.helpline)


def handle_account_question(ctx: Ctx, task: Task) -> HandlerOut:
    """Any other question about the verified loan, answered only from its allow-listed facts."""
    from servicing_agent.agent.output_guard import check

    facts = ctx.gw.account_facts()
    shared = _shared_payments(ctx)
    if shared and re.search(r"\b(sent|shared|uploaded|utr|receipt|screenshot)\b|\bamount (i|that i) paid\b",
                            ctx.summary_text or "", re.I):
        p = shared[0]
        status = {"MATCHED": " It's confirmed.", "ALREADY_CONFIRMED": " It's confirmed."}.get(p.get("result") or "", "")
        ctx.cite("CTX-01")
        return HandlerOut([render("context_payment_recall", ctx.lang, amount=money(p["amount"]) if p.get("amount") else "-",
                                  utr4=p["utr_last4"], date=p.get("date") or "-", status=status)])
    if shared:
        facts = {**facts, "payments_shared_in_this_chat": shared}
    if ctx.answerer is not None:
        draft = ctx.answerer.answer(ctx.summary_text, facts)
        if draft:
            g = check(draft, verified_account=ctx.gw.scope.account_id, allowed_numbers=ctx.gw.allowed_numbers,
                      allowed_strings=ctx.gw.allowed_strings, constants={ctx.settings.helpline})
            if g.ok:
                return HandlerOut([Reply(text=draft, template_id="account_answer_llm")])
            ctx.gw._event(EventType.OUTPUT_BLOCKED, reason=g.reason, detail=g.detail, template_id="account_answer_llm")
    return HandlerOut([_facts_reply(ctx, facts)])


def _shared_payments(ctx: Ctx) -> list[dict]:
    """Payments the borrower shared for the loan verified now (claims ledger, last 24 h, masked UTR)."""
    if ctx.gw.scope is None:
        return []
    return [{"utr_last4": r["utr"][-4:], "amount": r.get("amount"), "date": r.get("txn_date"), "result": r.get("result")}
            for r in ctx.gw.recent_claims(minutes=24 * 60) if r.get("utr")]


def handle_restricted(ctx: Ctx, task: Task) -> HandlerOut:
    ctx.cite("PRV-01")
    return HandlerOut([render("restricted_info", ctx.lang)])


POLICY_MIN_SCORE = 0.30          # hybrid score; off-topic questions score ~0.25, paraphrases >= 0.35


def handle_policy_question(ctx: Ctx, task: Task) -> HandlerOut:
    """'How does approval work?', 'Will a settlement affect my credit score?': answered with the
    borrower lines of the retrieved policy clauses, word for word."""
    from servicing_agent.agent.policy import policy
    if task.slots.get("topic"):
        return _explain_topic(ctx, task.slots["topic"])
    hits = [h for h in policy().search(ctx.summary_text, with_borrower_line=True) if h.score >= POLICY_MIN_SCORE
            and (h.bm25 > 0 or h.cosine > 0)]
    # The best clause, plus a close runner-up only if it also shares words with the question.
    found = hits[:1] + [h for h in hits[1:2] if h.bm25 > 0 and h.score >= 0.8 * hits[0].score]
    ctx.gw._event(EventType.TOOL_CALL, tool="policy.search", mode="hybrid" if policy().embedder else "bm25",
                  hits=[{"id": h.clause.id, "bm25": round(h.bm25, 2), "cosine": round(h.cosine, 3),
                         "score": round(h.score, 3)} for h in hits[:3]])
    if not found:
        return HandlerOut([render("clarify", ctx.lang)], resolution="clarify")
    found = sorted((h.clause for h in found), key=lambda c: c.order)
    ctx.cite(*[c.id for c in found])
    return HandlerOut([render("policy_answer", ctx.lang, answer=" ".join(c.borrower for c in found))])


def handle_out_of_scope(ctx: Ctx, task: Task) -> HandlerOut:
    return HandlerOut([render("out_of_scope", ctx.lang)])


def handle_charges(ctx: Ctx, task: Task) -> HandlerOut:
    c = ctx.gw.charges()
    if not c["total_charges"]:
        return HandlerOut([render("charges_none", ctx.lang)])
    return HandlerOut([render("charges", ctx.lang, bounce=money(c["bounce_charges"]), penal=money(c["penal_charges"]),
                              total=money(c["total_charges"]))])


def handle_receipt(ctx: Ctx, task: Task) -> HandlerOut:
    held = _sim_swap_hold(ctx, "receipt")
    if held:
        return held
    utr = task.slots.get("utr")
    if not utr:
        last = ctx.gw.last_successful_payment()
        if not last:
            return HandlerOut([render("claim_need_utr", ctx.lang)], resolution="clarify")
        utr = last["utr"]
    try:
        r = ctx.gw.receipt(utr)
    except (KeyError, AssertionError):
        # UTR not on this account: never confirm or deny what it belongs to.
        return HandlerOut([render("claim_need_utr", ctx.lang)], resolution="clarify")
    return HandlerOut([render("receipt_sent", ctx.lang, amount=money(r["amount"]), payment_date=r["payment_date"],
                              utr=r["utr"], attachments=[r["document_id"]])])


def handle_statement(ctx: Ctx, task: Task) -> HandlerOut:
    held = _sim_swap_hold(ctx, "statement")
    if held:
        return held
    to = date.today()
    frm = to - timedelta(days=180)
    s = ctx.gw.statement(frm.isoformat(), to.isoformat())
    return HandlerOut([render("statement_sent", ctx.lang, period_from=s["period_from"], period_to=s["period_to"],
                              attachments=[s["document_id"]])])


def handle_payment_claim(ctx: Ctx, task: Task) -> HandlerOut:
    """Payment claim, decided in real time (demo window: SA_CLAIM_WINDOW_SECONDS, default 2 minutes).

    * matches the ledger -> confirmation + receipt
    * contradicts the ledger or scores as likely fake -> fraud ticket raised silently; the borrower
      gets a neutral "couldn't verify this receipt" (never told it was flagged or escalated)
    * not in the ledger yet -> "checking, I'll confirm in 2 minutes"; one recheck when the window
      ends: matched -> confirmation, still missing -> "couldn't confirm, escalation filed" + reference
    Every claim is written to the payment_claims ledger; resends never open a second ticket.
    """
    s = task.slots
    recheck = bool(s.get("recheck"))
    from_context = bool(s.get("from_context"))
    if ctx.memory and s.get("utr") and not from_context and not recheck:
        ctx.memory.remember_payment(ctx.session, s)
    res = ctx.gw.reconcile_claim(s.get("utr"), s.get("amount"), s.get("txn_date"), s.get("payee_vpa"))
    if res.verdict == Verdict.NEED_UTR:
        # "I already paid / I already sent the UTR" (CTX-01): the latest claim on this loan from the claims
        # ledger (any session, last 24 h), re-checked against the ledger now. Never ask for the UTR again.
        last = ctx.gw.latest_claim(minutes=24 * 60)
        if last and (last.get("result") == "OTHER_ACCOUNT" or _before_reminder(ctx, last)):
            last = None          # already answered as another loan's, or sent before the reminder: ask afresh
        if last and last.get("utr") and not from_context:
            slots = {k: last.get(k) for k in ("utr", "amount", "txn_date", "payee_vpa", "media_id")}
            slots.update(source="context", from_context=True)
            ctx.cite("CTX-01")
            return handle_payment_claim(ctx, Task(intent=Intent.PAYMENT_CLAIM, confidence=1.0, slots=slots))
        if last:
            return _about_latest_claim(ctx, last)
        return HandlerOut([render("claim_need_utr", ctx.lang)], resolution="clarify")

    # ---- the same receipt again (DUP-01): no second ticket or check; a hard stop after max_resends
    prev = ctx.gw.previous_claim(s.get("utr"))
    # A resend is the SAME receipt (same UTR and amount); the same UTR with different details is new evidence.
    resend = bool(prev) and not recheck and not from_context and _same_receipt(prev, s) and \
        res.verdict not in (Verdict.MATCHED, Verdict.ALREADY_CONFIRMED)
    if resend and (prev.get("attempts") or 0) >= resend_limit():
        return _resend_stopped(ctx, res, s, prev)

    # ---- decide by policy (config/policy/servicing_policy.md, decides: payment_claim)
    from servicing_agent.agent.policy import policy
    facts: dict = {"verdict": res.verdict.value}
    other = None
    if res.verdict == Verdict.OTHER_ACCOUNT:
        other = ctx.gw.borrowers_other_loan(s.get("utr"))
        facts["same_borrower_loan"] = bool(other)
    auth = None
    if res.verdict not in (Verdict.MATCHED, Verdict.ALREADY_CONFIRMED):
        auth = ctx.gw.assess_claim(s, res)
        facts["authenticity"] = auth.label
    decision = policy().decide("payment_claim", facts)
    if decision is None:                                    # no clause matched: be safe, escalate quietly
        log.error("no payment_claim clause matched %s", facts)
        decision = policy().decide("payment_claim", {"verdict": "MISMATCH"})
    act = decision.action
    ctx.cite(decision.clause.id, *(act.get("cite") or []))
    ctx.decision(decision)
    detail = _claim_detail(s)

    if act["action"] == "confirm":
        r = ctx.gw.receipt(res.txn["utr"])                 # same txn -> same document id
        ctx.gw.record_claim(res, s)
        ctx.gw.record_receipt(res.txn["txn_id"], r["document_id"])   # no-op if already issued
        ctx.claims_this_turn.append({"status": "matched", "amount": float(r["amount"])})
        if recheck and res.verdict == Verdict.ALREADY_CONFIRMED:
            return HandlerOut([], resolution="auto")       # confirmed meanwhile (e.g. a resend): say nothing
        tpl = "claim_matched" if res.verdict == Verdict.MATCHED else "claim_already_confirmed"
        # TODO(parked): emit dunning.suppress for this account.
        return HandlerOut([render(tpl, ctx.lang, amount=money(r["amount"]), payment_date=r["payment_date"],
                                  utr=r["utr"], attachments=[r["document_id"]])])

    if act["action"] == "suggest_switch":
        # Their own other loan: the wrong loan was picked, not a fake. No ticket.
        if s.get("amount") is not None:
            ctx.gw.allow(float(s["amount"]))               # the borrower's own figures, repeated back
        if s.get("utr"):
            ctx.gw.allow(s["utr"][-4:])
        ctx.gw.record_claim(res, s)
        ctx.claims_this_turn.append({"status": "other_loan"})
        rem = active_reminder(ctx)
        if rem:
            # REM-02: the conversation is about the reminder's loan. Say where the payment went, don't offer a
            # switch; what's due on this loan stays due, with a link to pay it.
            ctx.cite("REM-02")
            due = float(rem.get("amount") or 0)
            ctx.gw.allow(due)
            link = ctx.gw.payment_link(due)
            return HandlerOut([render("claim_other_loan_reminder", ctx.lang, detail=detail, other=other,
                                      current=ctx.gw.mask_account(ctx.gw.scope.account_id), amount=money(due),
                                      payment_link=link["payment_link"])], resolution="pending")
        return HandlerOut([render("claim_other_own_loan", ctx.lang, detail=detail, other=other,
                                  current=ctx.gw.mask_account(ctx.gw.scope.account_id))], resolution="pending")

    # Only a live payment ticket for this same outcome counts (not a stale reference, not a genuine earlier claim).
    already_ticketed = bool(prev and prev.get("result") == res.verdict.value and _payment_ticket(ctx, prev.get("ticket_id")))

    if act["action"] == "confirm_then_escalate" and not recheck:
        # CLM-03: the details don't match the ledger. Ask the borrower to double-check before anything is raised.
        ctx.gw.record_claim(res, s)
        ctx.claims_this_turn.append({"status": "unverified"})
        if already_ticketed:                                # the same flagged receipt again: neutral, no new ticket
            if from_context:
                return HandlerOut([render("claim_latest_unverified", ctx.lang, detail=_claim_detail(s),
                                          helpline=ctx.settings.helpline)], resolution="routed")
            return HandlerOut([render("claim_unverified", ctx.lang)], resolution="routed")
        claimed = _claimed_text(s)
        ctx.gw.allow([x for x in (s.get("amount"), (s.get("utr") or "")[-4:] or None, s.get("txn_date")) if x])
        pending = {"kind": "confirm_receipt", "request": {"type": "payment_claim"},
                   "slots": {k: s.get(k) for k in ("utr", "amount", "txn_date", "txn_time", "payee_vpa", "source",
                                                   "media_id", "first_seen_at")},
                   "verdict": res.verdict.value, "reasons": list(res.reasons or []),
                   "authenticity": auth.as_dict() if auth else None, "decision": decision.record(policy().version),
                   "act": dict(act)}
        return HandlerOut([render("claim_confirm_receipt", ctx.lang, claimed=claimed)], resolution="pending",
                          pending_action=pending)

    if act["action"] in ("escalate", "confirm_then_escalate"):
        ctx.claims_this_turn.append({"status": "unverified"})
        tickets = []
        if already_ticketed:                               # same receipt again: no second ticket
            ctx.gw.record_claim(res, s)
        else:
            tickets = [_claim_ticket(ctx, res, s, auth, act, decision)]
        if recheck:
            return HandlerOut([], tickets, "routed")
        if act.get("notify_borrower", True):
            return HandlerOut([render("claim_escalated_notice", ctx.lang, detail=detail,
                                      ticket_id=(tickets[0] if tickets else prev)["ticket_id"])], tickets, "routed")
        # notify_borrower=false: neutral on purpose, the borrower is not told it was flagged or escalated.
        if from_context:
            return HandlerOut([render("claim_latest_unverified", ctx.lang, detail=detail,
                                      helpline=ctx.settings.helpline)], tickets, "routed")
        return HandlerOut([render("claim_unverified", ctx.lang)], tickets, "routed")

    # act["action"] in ("wait_then_escalate", "confirm_then_wait")
    window = claim_window(ctx, act)
    if already_ticketed:
        ctx.gw.record_claim(res, s)
        ctx.claims_this_turn.append({"status": "escalated"})
        if recheck:
            return HandlerOut([], resolution="routed")
        return HandlerOut([render("claim_review_existing", ctx.lang, ticket_id=prev["ticket_id"])], resolution="routed")

    if prev and not recheck and (from_context or resend):
        # A check for this payment is already running: say how long is left, don't start another one.
        left = window - _seconds_since(prev.get("created_at"), ctx.received_at)
        if from_context or left > -CHECK_GRACE_SECONDS:
            if resend:
                ctx.cite("DUP-01")
                ctx.gw.record_claim(res, s)
            ctx.claims_this_turn.append({"status": "checking"})
            return HandlerOut([render("claim_still_checking", ctx.lang, detail=detail,
                                      window=_window_text(max(left, 30)))], resolution="pending")

    if act["action"] == "confirm_then_wait" and not recheck and not s.get("confirmed") and not from_context \
            and getattr(ctx.settings, "claim_confirm_missing", True):
        # CLM-02: not in our records. Ask the borrower to double-check before the window starts; nothing is raised yet.
        ctx.claims_this_turn.append({"status": "unconfirmed"})
        ctx.gw.allow([x for x in (s.get("amount"), (s.get("utr") or "")[-4:] or None, s.get("txn_date")) if x])
        pending = {"kind": "confirm_receipt", "mode": "missing", "request": {"type": "payment_claim"},
                   "slots": {k: v for k, v in s.items() if v is not None and k not in ("recheck", "from_context")},
                   "verdict": res.verdict.value, "act": dict(act)}
        what = "UTR" if (s.get("source") or "text") == "text" else "screenshot"
        return HandlerOut([render("claim_confirm_missing", ctx.lang, claimed=_claimed_text(s), what=what)],
                          resolution="pending", pending_action=pending)

    if not recheck:
        # Not in the ledger yet: give it the window, then look again.
        ctx.gw.record_claim(res, s)
        ctx.claims_this_turn.append({"status": "checking"})
        scope = ctx.gw.scope
        follow = {"kind": "recheck_claim", "delay_seconds": window,
                  "account_id": scope.account_id, "customer_id": scope.customer_id,
                  "slots": {**s, "recheck": True, "first_seen_at": (ctx.received_at or datetime.utcnow())
                            .isoformat(timespec="seconds")}}
        return HandlerOut([render("claim_checking", ctx.lang, detail=detail, window=_window_text(window))],
                          resolution="pending", followups=[follow])

    # The window has passed and it's still not in the ledger: escalate as the clause says.
    t = _claim_ticket(ctx, res, s, auth, act, decision)
    ctx.claims_this_turn.append({"status": "escalated"})
    if not act.get("notify_borrower", True):
        return HandlerOut([], [t], "routed")
    from servicing_agent.agent.tools.reconciliation import to_india
    checked = f" at {to_india(datetime.utcnow()).strftime('%H:%M')} IST"
    ctx.gw.allow(to_india(datetime.utcnow()).strftime('%H:%M'))
    return HandlerOut([render("claim_failed_escalated", ctx.lang, detail=detail, window=_window_text(window), checked=checked,
                              ticket_id=t["ticket_id"])], [t], "routed")


CHECK_GRACE_SECONDS = 60       # a recheck due this long ago may still be running


def _limit(clause_id: str, key: str, default: int) -> int:
    from servicing_agent.agent.policy import policy
    try:
        return int(policy().get(clause_id).action.get(key, default))
    except StopIteration:
        return default


def resend_limit() -> int:
    """How many times the same receipt is processed before the hard stop (policy DUP-01)."""
    return _limit("DUP-01", "max_resends", 3)


def revision_limit() -> int:
    """How many times an open waiver / settlement request may be changed (policy DUP-02)."""
    return _limit("DUP-02", "max_revisions", 2)


def _seconds_since(iso: str | None, now: datetime | None) -> int:
    if not iso:
        return 10 ** 6
    try:
        return int(((now or datetime.utcnow()) - datetime.fromisoformat(iso)).total_seconds())
    except ValueError:
        return 10 ** 6


def _resend_stopped(ctx: Ctx, res, s: dict, prev: dict) -> HandlerOut:
    """Hard stop: this receipt was already sent max_resends times. Nothing is re-run; the attempt is logged."""
    ctx.cite("DUP-01")
    ctx.gw.record_claim(res, s)                            # audit: attempts keeps counting
    ctx.gw._event(EventType.TOOL_CALL, tool="claims.resend_limit", attempts=(prev.get("attempts") or 0) + 1,
                  limit=resend_limit(), ticket_id=prev.get("ticket_id"))
    ctx.claims_this_turn.append({"status": "unverified"})
    if prev.get("result") in {v.value for v in FLAGGED} or res.verdict in FLAGGED:   # stay neutral (CLM-03/04)
        ctx.cite("CLM-04")
        return HandlerOut([render("claim_resend_limit_neutral", ctx.lang, helpline=ctx.settings.helpline)],
                          resolution="routed")
    detail = _claim_detail(s)
    ctx.gw.allow([x for x in (s.get("amount"), (s.get("utr") or "")[-4:] or None) if x is not None])
    ref = prev.get("ticket_id") if _payment_ticket(ctx, prev.get("ticket_id")) else None
    status = f" It's with our team under reference {ref}." if ref else " We're checking it."
    return HandlerOut([render("claim_resend_limit", ctx.lang, detail=detail, status=status)], resolution="routed")


def _same_receipt(prev: dict, s: dict) -> bool:
    a, b = prev.get("amount"), s.get("amount")
    return a is None or b is None or abs(float(a) - float(b)) < 0.5


def _payment_ticket(ctx: Ctx, ticket_id: str | None) -> bool:
    """A real payment ticket? (References stored before DB ticketing may point at an unrelated ticket now.)"""
    if not ticket_id:
        return False
    getter = getattr(ctx.gw.tools.tickets, "request_type_of", None)
    rtype = getter(ticket_id) if getter else "payment_"
    return bool(rtype and rtype.startswith("payment_"))


def _claimed_text(s: dict) -> str:
    parts = []
    if s.get("amount") is not None:
        parts.append(f"₹{money(s['amount'])}")
    if s.get("utr"):
        parts.append(f"UTR ending {s['utr'][-4:]}")
    if s.get("txn_date"):
        parts.append(f"dated {s['txn_date']}")
    return ", ".join(parts) or "this receipt"


def escalate_confirmed_receipt(ctx: Ctx, action: dict) -> HandlerOut:
    """YES, it's the right screenshot: a compliance ticket, categorised as a suspected fake or a record
    discrepancy. A suspected fake stays neutral to the borrower (no reference); a discrepancy gets the reference."""
    s = dict(action.get("slots") or {})
    auth = action.get("authenticity") or {}
    verdict = action.get("verdict")
    category = "suspected_fake" if (auth.get("label") == "likely_fake" or verdict in ("OTHER_ACCOUNT", "REPLAY")) \
        else "data_discrepancy"
    act = action.get("act") or {}
    ctx.cite("CLM-03", "CLM-04", "ESC-01")
    if action.get("decision"):
        ctx.gw.decisions.append({**action["decision"], "borrower_confirmed": True})
    summary = {"intent": "payment_claim", "verdict": verdict, "reasons": action.get("reasons"),
               "source": s.get("source") or "text", "category": category, "borrower_confirmed_screenshot": True,
               "claimed": {k: s.get(k) for k in ("utr", "amount", "txn_date", "txn_time", "payee_vpa")},
               "authenticity": auth or None, "flag": "SUSPECTED_MISMATCH" if category == "suspected_fake" else None,
               "borrower_informed": category != "suspected_fake", "triage": ctx.triage_info()}
    t = ctx.gw.ticket(act.get("ticket") or "payment_mismatch", summary)
    if ctx.gw.claims:
        ctx.gw.claims.record(conversation_id=ctx.conversation_id, account_id=ctx.gw.scope.account_id,
                             utr=s.get("utr"), result=verdict or "MISMATCH", source=s.get("source") or "text",
                             message_id=ctx.message_id, amount=s.get("amount"), ticket_id=t["ticket_id"],
                             queue=t.get("queue"), count_attempt=False)
    if category == "suspected_fake" and not act.get("notify_borrower", False):
        return HandlerOut([render("claim_confirmed_checking", ctx.lang)], [t], "routed")
    return HandlerOut([render("claim_confirmed_discrepancy", ctx.lang, ticket_id=t["ticket_id"])], [t], "routed")


def confirm_missing_receipt(ctx: Ctx, action: dict) -> HandlerOut:
    """YES to a payment not in our records (CLM-02): treated as money sent but not received. The claim is checked
    again now (it may have reached us meanwhile), then given the reflection window; still missing -> compliance."""
    slots = {**(action.get("slots") or {}), "confirmed": True}
    slots.pop("first_seen_at", None)
    return handle_payment_claim(ctx, Task(intent=Intent.PAYMENT_CLAIM, confidence=1.0, slots=slots))


def receipt_not_confirmed(ctx: Ctx, action: dict) -> HandlerOut:
    """NO, not the right screenshot: nothing is raised; ask for the correct one."""
    ctx.cite("CLM-02" if action.get("mode") == "missing" else "CLM-03")
    return HandlerOut([render("claim_send_another", ctx.lang)], resolution="clarify")


REMINDER_PIN_HOURS = 24


def active_reminder(ctx: Ctx) -> dict | None:
    """The payment reminder for the loan this conversation is verified for, if sent in the last 24 h (REM-02)."""
    rem = ctx.reminder or {}
    if not rem.get("account_id") or rem["account_id"] != (ctx.gw.scope.account_id if ctx.gw.scope else None):
        return None
    if _seconds_since(rem.get("sent_at"), datetime.utcnow()) > REMINDER_PIN_HOURS * 3600:
        return None
    return rem


def _before_reminder(ctx: Ctx, claim: dict) -> bool:
    """'Already paid' after a payment reminder is about a new payment: a claim from before the reminder isn't it."""
    rem = ctx.reminder or {}
    if rem.get("account_id") != (ctx.gw.scope.account_id if ctx.gw.scope else None) or not rem.get("sent_at"):
        return False
    return (claim.get("updated_at") or "") < rem["sent_at"]


def claim_window(ctx: Ctx, act: dict | None = None) -> int:
    """Seconds a payment not yet in the ledger is given: SA_CLAIM_WINDOW_SECONDS if set, else the policy."""
    if ctx.settings.claim_window_seconds:
        return int(ctx.settings.claim_window_seconds)
    if act is None:
        from servicing_agent.agent.policy import policy
        act = policy().get("CLM-02").action
    return int(act.get("wait_seconds", 120))


def _about_latest_claim(ctx: Ctx, last: dict) -> HandlerOut:
    detail = _claim_detail({"amount": last.get("amount"), "utr": last.get("utr")})
    result, ticket = last.get("result"), last.get("ticket_id")
    if result == "MATCHED":
        ctx.cite("CLM-01")
        return HandlerOut([render("claim_latest_confirmed", ctx.lang, detail=detail)])
    if result == "OTHER_ACCOUNT" and not ticket:
        ctx.cite("CLM-05")
        return HandlerOut([render("claim_latest_other_loan", ctx.lang, detail=detail)], resolution="pending")
    if ticket and result in {v.value for v in FLAGGED}:
        ctx.cite("CLM-03", "CLM-04")                      # still neutral: never reveal the review
        return HandlerOut([render("claim_latest_unverified", ctx.lang, detail=detail, helpline=ctx.settings.helpline)])
    if ticket:
        ctx.cite("CLM-02")
        return HandlerOut([render("claim_review_existing", ctx.lang, ticket_id=ticket)], resolution="routed")
    ctx.cite("CLM-02")
    return HandlerOut([render("claim_still_checking", ctx.lang, detail=detail,
                              window=_window_text(claim_window(ctx)))], resolution="pending")


def _window_text(seconds: int) -> str:
    if seconds >= 60:
        m = round(seconds / 60)
        return "1 minute" if m == 1 else f"{m} minutes"
    return "1 second" if seconds == 1 else f"{seconds} seconds"


def _claim_ticket(ctx: Ctx, res, s: dict, auth, act: dict, decision) -> dict:
    silent = not act.get("notify_borrower", True)
    summary = {"intent": "payment_claim", "verdict": res.verdict.value, "reasons": res.reasons,
               "source": s.get("source") or "text",
               "claimed": {k: s.get(k) for k in ("utr", "amount", "txn_date", "txn_time", "payee_vpa")},
               "authenticity": auth.as_dict() if auth else None,
               "payment_age_minutes": payment_age_minutes(s, ctx.received_at),
               "first_seen_at": s.get("first_seen_at"), "rechecked": bool(s.get("recheck")),
               "flag": "SUSPECTED_MISMATCH" if silent else None,
               "borrower_informed": not silent, "triage": ctx.triage_info()}
    if (act.get("ticket") or "payment_unmatched") == "payment_unmatched":
        # Not contradicted by the ledger, just missing: money the borrower says was sent but hasn't reached us.
        summary["category"] = "payment_not_received"
        summary["borrower_confirmed_screenshot"] = bool(s.get("confirmed"))
    t = ctx.gw.ticket(act.get("ticket") or "payment_unmatched", summary)
    ctx.gw.record_claim(res, s, ticket=t)
    return t

def payment_age_minutes(slots: dict, received_at: datetime | None) -> int | None:
    """Minutes between the payment (receipt date + time, India time) and the message. None if unknown."""
    d = slots.get("txn_date")
    if not d:
        return None
    now = to_india(received_at or datetime.utcnow())
    try:
        day = date.fromisoformat(d[:10])
    except ValueError:
        return None
    t = slots.get("txn_time")
    if t:
        try:
            hh, mm = (int(x) for x in t.split(":")[:2])
            paid_at = datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)
        except ValueError:
            paid_at = None
    else:
        paid_at = None
    if paid_at is None:
        if day >= now.date():
            return None                               # today, time unknown
        paid_at = datetime.combine(day, datetime.max.time())
    return max(0, int((now - paid_at).total_seconds() // 60))


def _claim_detail(s: dict) -> str:
    parts = []
    if s.get("amount") is not None:
        parts.append(f" of ₹{money(s['amount'])}")
    if s.get("utr") and len(s["utr"]) >= 4:
        parts.append(f" (UTR ending {s['utr'][-4:]})")
    return "".join(parts)


def _desc(req: dict) -> str:
    from servicing_agent.agent.planner import REASON_LABELS
    kind = req.get("type", "waiver")
    amt = f"₹{money(req['amount'])}" if req.get("amount") else "an amount to be confirmed"
    comps = f" on {', '.join(req['components'])} charges" if req.get("components") else ""
    head = f"a settlement where you pay {amt}" if kind == "settlement" else f"a {kind} of {amt}{comps}"
    reason = req.get("reason_category")
    return head + (f" (reason: {REASON_LABELS.get(reason, reason)})" if reason else "")


# ---- request details: amount and reason are agreed with the borrower before the YES / NO (APR-04)
MAX_DETAIL_ASKS = 2


def _missing_details(req: CaseRequest) -> list[str]:
    missing = []
    if req.amount is None and not (req.type.value == "waiver" and req.components):
        missing.append("amount")
    if not req.reason_category:
        missing.append("reason")
    return missing


def _ask_details(ctx: Ctx, req: CaseRequest, missing: list[str], state: dict) -> HandlerOut:
    kind = req.type.value
    qs = []
    if "amount" in missing:
        if kind == "settlement":
            d = ctx.gw.dues(Intent.EMI_DUE_QUERY)
            due = float(d["overdue_amount"] or 0) or float(d["emi_amount"] or 0)
            ctx.gw.allow(due)
            qs.append(render("case_ask_settlement_amount", ctx.lang, due=money(due)).text)
        else:
            qs.append(render("case_ask_waiver_amount", ctx.lang).text)
    if "reason" in missing:
        qs.append(render("case_ask_reason", ctx.lang).text)
    ctx.cite("APR-04")
    ctx.gw.allow(5000.0)                                    # the figure in the example answer below
    example = {("amount", "reason"): "₹5,000, I lost my job", ("reason",): "I was in hospital",
               ("amount",): "₹5,000"}[tuple(missing)] if kind == "settlement" else \
        {("amount", "reason"): "the late charges, I was in hospital", ("reason",): "my salary was delayed",
         ("amount",): "the late charges"}[tuple(missing)]
    replies = [render("case_details_needed", ctx.lang, kind=kind, questions="\n".join(f"• {q}" for q in qs),
                      example=example)]
    if ctx.hardship and not state.get("asked"):
        ctx.cite("HRD-01")
        replies.insert(0, render("hardship_ack", ctx.lang))
    # The first message's triage (hardship, priority) carries to the ticket, whatever the later answers say.
    triage_info = state.get("triage") or ctx.triage_info()
    return HandlerOut(replies, resolution="pending",
                      pending_action={**state, "kind": "case_details", "request": req.model_dump(mode="json"),
                                      "missing": missing, "triage": triage_info})


def fill_case_details(ctx: Ctx, action: dict, cancel: bool = False) -> HandlerOut:
    """The borrower's answer to the details question: amount and / or reason, then the usual YES / NO."""
    from servicing_agent.agent.planner import extract_any_amount, extract_reason
    if cancel:
        return HandlerOut([render("case_cancelled", ctx.lang)], resolution="auto")
    req = CaseRequest(**action["request"])
    text = ctx.summary_text or ""
    update: dict = {}
    if req.amount is None:
        amt = extract_any_amount(text)
        if amt:
            update["amount"] = amt
    if req.type.value == "waiver" and not req.components:
        comps = [c for c in ("penal", "bounce", "interest", "principal") if c in text.lower()]
        if not comps and re.search(r"late (fee|charge)|penalty", text, re.I):
            comps = ["penal"]
        if comps:
            update["components"] = comps
    if not req.reason_category:
        reason = extract_reason(text)
        # Anything else they wrote, beyond the amount, is their reason in their own words.
        words = re.sub(r"(₹|rs\.?|inr)?\s*[\d,]+(\.\d+)?|\b(rupees|only|the|amount|is|my|i|can|pay|charges?)\b", " ",
                       text, flags=re.I)
        if reason is None and len(re.findall(r"[a-zA-Z]{3,}", words)) >= 2:
            reason = "other"
        if reason:
            update["reason_category"] = reason
    req = req.model_copy(update={**update, "borrower_text": f"{req.borrower_text or ''} | {text}"[:500]})
    missing = _missing_details(req)
    if missing:
        asked = int(action.get("asked", 0)) + 1
        if asked >= MAX_DETAIL_ASKS:
            return HandlerOut([render("case_details_incomplete", ctx.lang, kind=req.type.value)], resolution="auto")
        return _ask_details(ctx, req, missing, {**action, "asked": asked})
    if action.get("change"):
        return _confirm_change_of(ctx, action["change"], req.model_dump(mode="json"))
    return _confirm_case(ctx, req, action.get("partial_payment"), action.get("tickets"), action.get("decision"),
                         triage_info=action.get("triage"), acknowledged=True)


def handle_case_request(ctx: Ctx, task: Task) -> HandlerOut:
    if task.slots.get("case_details"):
        return fill_case_details(ctx, task.slots["case_details"], cancel=bool(task.slots.get("cancel")))
    req = CaseRequest(**{k: v for k, v in task.slots.items() if k in CaseRequest.model_fields})
    if req.amount is None and ctx.memory:
        # "I can only pay 5000" earlier in this session, then WAIVER / SETTLE: the rest is what's asked for.
        part = next((e for e in ctx.memory.window(ctx.session, limit=6) if e.get("kind") == "part_payment"), None)
        if part:
            req = req.model_copy(update={"amount": float(part["amount"] if req.type.value == "settlement"
                                                         else part["shortfall"])})
    ctx.gw.allow(req.amount)
    open_case = ctx.gw.open_case()
    if open_case is None:
        held = None if ctx.claims_this_turn else _ladder_allows(ctx, req.type.value, ctx.summary_text or "")
        if held is not None:
            return held
        if ctx.settings.hardship_ladder:
            state = _ladder_state(ctx)
            from servicing_agent.agent.planner import extract_any_amount
            upd = {}
            if req.amount is None and state.get("offered") == req.type.value:
                amt = extract_any_amount(ctx.summary_text or "")     # "SETTLE 3000" right after the offer
                if amt:
                    upd["amount"] = amt
            if req.amount is None and req.type.value == "waiver" and state.get("rest"):
                upd["amount"] = float(state["rest"])        # the rest of the overdue, as offered
            if not req.reason_category and state.get("reason"):
                upd["reason_category"] = state["reason"]
            if upd:
                req = req.model_copy(update=upd)
        return _propose_case(ctx, req)

    old = open_case.request.model_dump(mode="json")
    new = req.model_dump(mode="json")
    # Fields the borrower did not restate are carried over, so "please approve fast" is not a change.
    for k in ("amount", "plan", "reason_category"):
        if new.get(k) is None:
            new[k] = old.get(k)
    if not new.get("components"):
        new["components"] = old.get("components", [])
    new["borrower_text"] = old.get("borrower_text")
    stage = STATUS_TEXT.get(open_case.status.value, "under review")
    ctx.cite("DUP-02", "APR-01")
    if not is_material_change(old, new):
        # Same request again: tell them it's already in progress and where. Nothing new is raised.
        return HandlerOut([render("case_active", ctx.lang, desc=_desc(old), case_id=open_case.case_id, stage=stage)],
                          resolution="routed")

    restarts = ctx.gw.case_restarts(open_case.case_id)
    limit = revision_limit()
    if restarts >= limit:
        # Hard stop: the request has been changed the maximum number of times; it stays as it is.
        ctx.gw._event(EventType.TOOL_CALL, tool="cases.revision_limit", case_id=open_case.case_id,
                      restarts=restarts, limit=limit)
        return HandlerOut([render("case_revision_limit", ctx.lang, case_id=open_case.case_id, desc=_desc(old),
                                  stage=stage, helpline=ctx.settings.helpline)], resolution="routed")

    new["borrower_text"] = req.borrower_text
    change = {"case_id": open_case.case_id, "from_version": open_case.version, "old": old}
    missing = _missing_details(CaseRequest(**new))
    if missing:                                            # the new version needs its amount / reason too
        return _ask_details(ctx, CaseRequest(**new), missing, {"change": change, "asked": 0})
    return _confirm_change_of(ctx, change, new, restarts, limit)


def _confirm_change_of(ctx: Ctx, change: dict, new: dict, restarts: int | None = None,
                       limit: int | None = None) -> HandlerOut:
    """Every detail of the changed request is agreed: explain that it restarts approval, then YES / NO."""
    case_id, old = change["case_id"], change["old"]
    restarts = ctx.gw.case_restarts(case_id) if restarts is None else restarts
    limit = revision_limit() if limit is None else limit
    stage = STATUS_TEXT.get((ctx.gw.open_case().status.value if ctx.gw.open_case() else ""), "under review")
    ctx.gw.allow(new.get("amount"))
    if ctx.memory:
        ctx.memory.remember(ctx.session, "proposal", {"type": new.get("type"), "amount": new.get("amount")})
    pending = PendingChange(case_id=case_id, from_version=change["from_version"], request=new)
    left = limit - restarts - 1
    note = (" This is the last change you can make to it." if left == 0 else "")
    reply = render("waiver_confirm_change", ctx.lang, old_desc=_desc(old), new_desc=_desc(new),
                   case_id=case_id, stage=stage, note=note, buttons=["Yes", "No"])
    return HandlerOut([reply], resolution="routed", pending_change=pending)


def _propose_case(ctx: Ctx, req: CaseRequest) -> HandlerOut:
    """A waiver / settlement ask. Code works out the facts (was a payment shared this turn, did it match,
    does it cover what's due); the policy document (decides: case_request) decides what happens."""
    from servicing_agent.agent.policy import clauses_for_case, consequences, policy
    kind = req.type.value
    facts = {"request": kind, "payment": "none", "coverage": "none"}
    paid = due = None
    if ctx.claims_this_turn:
        if any(c["status"] != "matched" for c in ctx.claims_this_turn):
            facts["payment"] = "unconfirmed"
        else:
            paid = round(sum(c["amount"] for c in ctx.claims_this_turn), 2)
            d = ctx.gw.dues(Intent.EMI_DUE_QUERY)
            due = float(d["overdue_amount"] or 0) or float(d["emi_amount"] or 0)
            facts.update(payment="matched", coverage="full" if paid >= due - 0.5 else "partial")
    decision = policy().decide("case_request", facts)
    act = decision.action
    ctx.cite(decision.clause.id, *(act.get("cite") or []))
    ctx.decision(decision)

    if act["action"] == "wait_for_payment":
        return HandlerOut([render("case_waits_for_payment", ctx.lang, kind=kind)], resolution="pending")
    if act["action"] == "not_needed":
        return HandlerOut([render("case_not_needed", ctx.lang, paid=money(paid), due=money(due),
                                  verb="waive" if kind == "waiver" else "settle")])

    # confirm_then_submit: agree the amount and reason first (APR-04), then explain (borrower lines retrieved
    # from the policy) and ask YES / NO.
    partial = None
    if facts["coverage"] == "partial":
        shortfall = round(due - paid, 2)
        if req.amount is None and act.get("amount_default") == "shortfall":
            req = req.model_copy(update={"amount": shortfall})
        partial = {"paid": paid, "due": due, "shortfall": shortfall}
        ctx.gw.allow([paid, due, shortfall])
    tickets = act.get("tickets") or ["approval"]
    record = decision.record(policy().version)
    missing = _missing_details(req)
    if missing:
        return _ask_details(ctx, req, missing, {"partial_payment": partial, "tickets": tickets, "decision": record,
                                                "asked": 0})
    return _confirm_case(ctx, req, partial, tickets, record)


def _confirm_case(ctx: Ctx, req: CaseRequest, partial: dict | None, tickets: list | None,
                  record: dict | None, triage_info: dict | None = None, acknowledged: bool = False) -> HandlerOut:
    """Every detail is agreed: the consequences (policy borrower lines), then YES / NO."""
    from servicing_agent.agent.policy import clauses_for_case, consequences
    kind = req.type.value
    if partial:
        ctx.gw.allow([partial["paid"], partial["due"], partial["shortfall"]])
    ctx.gw.allow(req.amount)
    desc = _desc(req.model_dump(mode="json"))
    lines = consequences(kind, partial=bool(partial))
    bullets = "\n".join(f"• {c.borrower}" for c in lines)
    ctx.cite(*[c.id for c in clauses_for_case(kind, partial=bool(partial))], *[c.id for c in lines])
    if partial:
        reply = render("case_confirm_partial", ctx.lang, paid=money(partial["paid"]), due=money(partial["due"]),
                       shortfall=money(partial["shortfall"]), desc=desc, consequences=bullets, buttons=["Yes", "No"])
    else:
        reply = render("case_confirm", ctx.lang, desc=desc, consequences=bullets, buttons=["Yes", "No"])
    replies = [reply]
    if ctx.hardship and not acknowledged:
        ctx.cite("HRD-01")
        replies.insert(0, render("hardship_ack", ctx.lang))
    return HandlerOut(replies, resolution="pending",
                      pending_action={"kind": "submit_case", "request": req.model_dump(mode="json"),
                                      "partial_payment": partial, "triage": triage_info or ctx.triage_info(),
                                      "tickets": tickets or ["approval"], "decision": record,
                                      "policy_refs": list(ctx.gw.policy_refs)})


def submit_case(ctx: Ctx, action: dict) -> HandlerOut:
    """The borrower said YES. Raise the tickets the deciding clause listed (approval, compliance...)."""
    req = CaseRequest(**action["request"])
    if ctx.gw.open_case() is not None:                    # submitted meanwhile (e.g. a double YES)
        return HandlerOut([render("waiver_duplicate", ctx.lang, case_id=ctx.gw.open_case().case_id)], resolution="routed")
    ctx.cite(*(action.get("policy_refs") or []), "APR-03", "ESC-01")
    if action.get("decision"):
        ctx.gw.decisions.append({**action["decision"], "borrower_confirmed": True})
    case = ctx.gw.create_case(req)
    # Snapshot the band eligibility (same YAML the approver review uses) so the confirm-first flow and
    # the human review agree on within_band / credit-head, and the fact is stored, not recomputed live.
    try:
        eligibility = ctx.gw.cases.eligibility_for(case.case_id)
    except Exception:  # noqa: BLE001
        eligibility = None
    security = _sim_swap_review(ctx, f"{req.type.value} request", linked_case=case.case_id)
    summary = {"case_id": case.case_id, "version": case.version, "request": req.model_dump(mode="json"),
               "triage": action.get("triage") or ctx.triage_info(), "borrower_confirmed": True,
               "partial_payment": action.get("partial_payment"), "eligibility": eligibility,
               "security_check": ({"reason": "recent_contact_change", "ticket_id": security["ticket_id"]}
                                  if security else None)}
    tickets = []
    for kind in action.get("tickets") or ["approval"]:
        request_type = f"{req.type.value}_request" if kind == "approval" else kind
        extra = {"approval_ticket": tickets[0]["ticket_id"]} if tickets else {}
        tickets.append(ctx.gw.ticket(request_type, {**summary, **extra}))
    if ctx.memory:
        ctx.memory.remember(ctx.session, "request", {"type": req.type.value, "amount": req.amount,
                                                     "case_id": case.case_id})
    replies = [render("waiver_created", ctx.lang, kind=req.type.value, case_id=case.case_id)]
    if security:
        replies.append(render("sim_swap_case_note", ctx.lang, ticket_id=security["ticket_id"]))
        if security.pop("_new", False):
            tickets.append(security)
    return HandlerOut(replies, tickets, "routed")


def confirm_change(ctx: Ctx, pending: PendingChange, yes: bool) -> HandlerOut:
    if not yes:
        return HandlerOut([render("waiver_change_kept", ctx.lang, case_id=pending.case_id)], resolution="routed")
    ctx.cite("DUP-02", "APR-01")
    if ctx.gw.case_restarts(pending.case_id) >= revision_limit():   # changed elsewhere meanwhile
        return HandlerOut([render("waiver_change_kept", ctx.lang, case_id=pending.case_id)], resolution="routed")
    case = ctx.gw.supersede_case(pending.case_id, pending.from_version, CaseRequest(**pending.request), ctx.message_id)
    # The old version's reviewers stop: its open tickets are closed as superseded, and the new version
    # starts the approval again with the same tickets (approval first, then any compliance ticket).
    old = ctx.gw.retire_case_tickets(pending.case_id, case.version)
    kinds = list(dict.fromkeys(t["request_type"] for t in old)) or [f"{case.request.type.value}_request"]
    summary = {"case_id": case.case_id, "version": case.version, "request": case.request.model_dump(mode="json"),
               "diff": case.diff, "supersedes": [t["ticket_id"] for t in old], "borrower_confirmed": True,
               "triage": ctx.triage_info(), "restarted": True}
    tickets = []
    for kind in kinds:
        extra = {"approval_ticket": tickets[0]["ticket_id"]} if tickets else {}
        tickets.append(ctx.gw.ticket(kind, {**summary, **extra}))
    return HandlerOut([render("waiver_restarted", ctx.lang, case_id=case.case_id)], tickets, "routed")


def handle_case_status(ctx: Ctx, task: Task) -> HandlerOut:
    case = ctx.gw.open_case()
    if not case:
        return HandlerOut([render("no_open_case", ctx.lang)])
    if re.search(r"\b(more|details?|about|what (was|is) (my|the) request)\b", ctx.summary_text or "", re.I):
        return HandlerOut([render("case_status_detail", ctx.lang, case_id=case.case_id,
                                  desc=_desc(case.request.model_dump(mode="json")),
                                  status_text=STATUS_TEXT[case.status.value])], resolution="routed")
    return HandlerOut([render("case_status", ctx.lang, case_id=case.case_id,
                              status_text=STATUS_TEXT[case.status.value])], resolution="routed")


def handle_case_withdraw(ctx: Ctx, task: Task) -> HandlerOut:
    """'Cancel my request' -> confirm, then withdraw. 'Cancel it and start a new one for ₹X' -> a change of the
    open request (cancels it and restarts approval, after the usual details and YES)."""
    from servicing_agent.agent.planner import extract_any_amount, extract_case_request
    ctx.cite("DUP-02", "APR-01")
    case = ctx.gw.open_case()
    text = ctx.summary_text or ""
    named = "settlement" if re.search(r"\bsettle", text, re.I) else ("waiver" if re.search(r"\bwaive", text, re.I) else None)
    amount = extract_any_amount(text)
    if amount or (named and re.search(r"\b(new|fresh|instead|restart|re-?initiate|start)\b", text, re.I)) or case is None:
        if case is None and not (named or amount):
            return HandlerOut([render("no_open_case", ctx.lang)])
        # A new request: its type is what they said, else the one just discussed in this session, else the open one.
        recent = next((e for e in (ctx.memory.window(ctx.session, limit=8) if ctx.memory else [])
                       if e.get("kind") == "proposal"), None)
        kind = named or (recent or {}).get("type") or (case.request.type.value if case else "waiver")
        slots = {k: v for k, v in extract_case_request(text, Intent(kind + "_request")).items() if v not in (None, [])}
        slots["type"] = kind
        if amount:
            slots["amount"] = amount
        return handle_case_request(ctx, Task(intent=Intent(kind + "_request"), confidence=1.0, slots=slots))
    desc = _desc(case.request.model_dump(mode="json"))
    return HandlerOut([render("case_withdraw_confirm", ctx.lang, case_id=case.case_id, desc=desc, buttons=["Yes", "No"])],
                      resolution="pending",
                      pending_action={"kind": "withdraw_case", "request": {"type": case.request.type.value},
                                      "case_id": case.case_id, "version": case.version})


def withdraw_case(ctx: Ctx, action: dict) -> HandlerOut:
    """YES to the withdraw question: the case is withdrawn and its open tickets close (no one reviews it)."""
    ctx.cite("DUP-02")
    try:
        ctx.gw.withdraw_case(action["case_id"], action["version"])
    except Exception:  # noqa: BLE001 - decided or changed meanwhile
        case = ctx.gw.open_case()
        return HandlerOut([render("case_status", ctx.lang, case_id=action["case_id"],
                                  status_text=STATUS_TEXT.get(case.status.value, "under review") if case else "closed")],
                          resolution="routed")
    fn = getattr(ctx.gw.tools.tickets, "supersede_for_case", None)
    if fn:
        fn(ctx.gw.scope.account_id, action["case_id"], "Withdrawn by the borrower")
    return HandlerOut([render("case_withdrawn", ctx.lang, case_id=action["case_id"])], resolution="auto")


def handle_session_info(ctx: Ctx, task: Task) -> HandlerOut:
    """'Which loan am I logged in to?': the verified loan (masked), and when the session ends."""
    from datetime import timedelta

    from servicing_agent.agent.tools.reconciliation import to_india
    scope = ctx.gw.scope
    if scope is None:
        return HandlerOut([render("session_info_none", ctx.lang)])
    ends = min(scope.expires_at, scope.verified_at + timedelta(minutes=ctx.settings.session_max_minutes))
    product = str(ctx.gw.account_facts().get("product") or "").replace("_", "-") or "your"
    account = ctx.gw.mask_account(scope.account_id)
    end_txt = to_india(ends).strftime("%H:%M")
    ctx.gw.allow([account, end_txt, ctx.settings.session_idle_minutes])
    return HandlerOut([render("session_info", ctx.lang, account=account, product=product, ends=end_txt,
                              idle=ctx.settings.session_idle_minutes)])


def handle_faq(ctx: Ctx, task: Task) -> HandlerOut:
    return HandlerOut([render("faq", ctx.lang, helpline=ctx.settings.helpline)])


def handle_greeting(ctx: Ctx, task: Task) -> HandlerOut:
    return HandlerOut([render("greeting", ctx.lang)])


HANDLERS = {
    Intent.FORECLOSURE_QUOTE: handle_foreclosure,
    Intent.EMI_DUE_QUERY: handle_emi_due,
    Intent.PAY_NOW: handle_pay_now,
    Intent.PAYMENT_HISTORY: handle_payment_history,
    Intent.LOAN_SUMMARY: handle_loan_summary,
    Intent.CHARGES_QUERY: handle_charges,
    Intent.ACCOUNT_QUESTION: handle_account_question,
    Intent.RESTRICTED_INFO: handle_restricted,
    Intent.OUT_OF_SCOPE: handle_out_of_scope,
    Intent.POLICY_QUESTION: handle_policy_question,
    Intent.PAYMENT_DIFFICULTY: handle_payment_difficulty,
    Intent.ESCALATION_REQUEST: handle_escalation_request,
    Intent.RECEIPT_REQUEST: handle_receipt,
    Intent.STATEMENT_REQUEST: handle_statement,
    Intent.PAYMENT_CLAIM: handle_payment_claim,
    Intent.WAIVER_REQUEST: handle_case_request,
    Intent.SETTLEMENT_REQUEST: handle_case_request,
    Intent.CASE_STATUS: handle_case_status,
    Intent.CASE_WITHDRAW: handle_case_withdraw,
    Intent.SESSION_INFO: handle_session_info,
    Intent.FAQ: handle_faq,
    Intent.GREETING: handle_greeting,
}


def run_task(ctx: Ctx, task: Task) -> HandlerOut:
    if task.intent in HANDOFF_INTENTS:
        return _route(ctx, task.intent.value, task, handoff=True)
    if task.intent in (Intent.DISPUTE, Intent.COMPLAINT, Intent.CHARGE_REVERSAL):
        return _route(ctx, task.intent.value, task)
    handler = HANDLERS.get(task.intent)
    if handler is None:
        return HandlerOut([render("clarify", ctx.lang)], resolution="clarify")
    return handler(ctx, task)
