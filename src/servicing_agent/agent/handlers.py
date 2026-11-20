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
    """The borrower asks to escalate (policy ESC-02): a compliance_review ticket, linked to their latest payment
    claim if there is one, with the reference in the reply. Asking again returns the same reference. If that
    claim was escalated silently (CLM-03), the reply doesn't reveal the earlier review."""
    ctx.cite("ESC-02", "ESC-01")
    prior = ctx.gw.open_ticket("borrower_escalation")          # already escalated and still open: same reference
    if prior:
        return HandlerOut([render("escalation_existing", ctx.lang, ticket_id=prior["ticket_id"])], resolution="routed")
    last = None
    row = ctx.gw.latest_claim(minutes=24 * 60)                  # this loan's latest claim, any session
    if row:
        last = {"utr": row.get("utr"), "amount": row.get("amount"), "last_result": row.get("result"),
                "ticket_id": row.get("ticket_id")}
    silent = bool(last and last.get("last_result") in {v.value for v in FLAGGED})
    summary = {"intent": "escalation_request", "requested_by_borrower": True, "borrower_text": ctx.summary_text[:500],
               "payment": ({"utr_last4": (last.get("utr") or "")[-4:], "amount": last.get("amount"),
                            "result": last.get("last_result"), "linked_ticket": last.get("ticket_id")} if last else None),
               "earlier_review_silent": silent, "triage": ctx.triage_info()}
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
    borrower picks one; a waiver or settlement then goes through its own confirm-first flow."""
    d = ctx.gw.dues(Intent.EMI_DUE_QUERY)
    due = float(d["overdue_amount"] or 0) or float(d["emi_amount"] or 0)
    ctx.cite("HRD-02", "HRD-01")
    return HandlerOut([render("payment_difficulty", ctx.lang, due=money(due))], resolution="pending")


def handle_pay_now(ctx: Ctx, task: Task) -> HandlerOut:
    d = ctx.gw.dues(Intent.PAY_NOW)
    amount = task.slots.get("amount") or d["overdue_amount"] or d["emi_amount"]   # "I can pay 5000" -> 5000
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
    resend = bool(prev) and not recheck and not from_context and res.verdict not in (Verdict.MATCHED,
                                                                                      Verdict.ALREADY_CONFIRMED)
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
        return HandlerOut([render("claim_other_own_loan", ctx.lang, detail=detail, other=other,
                                  current=ctx.gw.mask_account(ctx.gw.scope.account_id))], resolution="pending")

    already_ticketed = bool(prev and prev.get("ticket_id"))

    if act["action"] == "escalate":
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

    # act["action"] == "wait_then_escalate"
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
    return HandlerOut([render("claim_failed_escalated", ctx.lang, detail=detail, window=_window_text(window),
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
    if prev.get("result") in {v.value for v in FLAGGED}:   # silent review: stay neutral (CLM-03, CLM-04)
        ctx.cite("CLM-04")
        return HandlerOut([render("claim_resend_limit_neutral", ctx.lang, helpline=ctx.settings.helpline)],
                          resolution="routed")
    detail = _claim_detail(s)
    ctx.gw.allow([x for x in (s.get("amount"), (s.get("utr") or "")[-4:] or None) if x is not None])
    status = (f" It's with our team under reference {prev['ticket_id']}." if prev.get("ticket_id")
              else " We're checking it.")
    return HandlerOut([render("claim_resend_limit", ctx.lang, detail=detail, status=status)], resolution="routed")


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
    kind = req.get("type", "waiver")
    amt = f"₹{money(req['amount'])}" if req.get("amount") else "an amount to be confirmed"
    comps = f" on {', '.join(req['components'])} charges" if req.get("components") else ""
    return f"a {kind} of {amt}{comps}"


def handle_case_request(ctx: Ctx, task: Task) -> HandlerOut:
    req = CaseRequest(**{k: v for k, v in task.slots.items() if k in CaseRequest.model_fields})
    ctx.gw.allow(req.amount)
    open_case = ctx.gw.open_case()
    if open_case is None:
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
    pending = PendingChange(case_id=open_case.case_id, from_version=open_case.version, request=new)
    left = limit - restarts - 1
    note = (" This is the last change you can make to it." if left == 0 else "")
    reply = render("waiver_confirm_change", ctx.lang, old_desc=_desc(old), new_desc=_desc(new),
                   case_id=open_case.case_id, stage=stage, note=note, buttons=["Yes", "No"])
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

    # confirm_then_submit: explain (borrower lines retrieved from the policy), then ask YES / NO.
    partial = None
    if facts["coverage"] == "partial":
        shortfall = round(due - paid, 2)
        if req.amount is None and act.get("amount_default") == "shortfall":
            req = req.model_copy(update={"amount": shortfall})
        partial = {"paid": paid, "due": due, "shortfall": shortfall}
        ctx.gw.allow([paid, due, shortfall])
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
    if ctx.hardship:
        ctx.cite("HRD-01")
        replies.insert(0, render("hardship_ack", ctx.lang))
    return HandlerOut(replies, resolution="pending",
                      pending_action={"kind": "submit_case", "request": req.model_dump(mode="json"),
                                      "partial_payment": partial, "triage": ctx.triage_info(),
                                      "tickets": act.get("tickets") or ["approval"],
                                      "decision": decision.record(policy().version),
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
    return HandlerOut([render("case_status", ctx.lang, case_id=case.case_id,
                              status_text=STATUS_TEXT[case.status.value])], resolution="routed")


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
