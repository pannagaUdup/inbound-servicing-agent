"""Intent handlers: code, not prompts. Each one calls tools only through the gateway."""

from __future__ import annotations

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

    def cite(self, *clause_ids: str):
        self.gw.cite(*clause_ids)

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


def handle_foreclosure(ctx: Ctx, task: Task) -> HandlerOut:
    q = ctx.gw.foreclosure_quote()
    b = q["breakup"]
    return HandlerOut([render("foreclosure_quote", ctx.lang, amount=money(q["amount"]), valid_until=q["valid_until"],
                              principal=money(b["principal"]), overdue=money(b["overdue"]),
                              charges=money(b["charges"]), foreclosure_fee=money(b["foreclosure_fee"]))])


def handle_emi_due(ctx: Ctx, task: Task) -> HandlerOut:
    d = ctx.gw.dues()
    return HandlerOut([render("emi_due", ctx.lang, emi_amount=money(d["emi_amount"]),
                              next_due_date=d["next_due_date"], overdue_amount=money(d["overdue_amount"]))])


def handle_pay_now(ctx: Ctx, task: Task) -> HandlerOut:
    d = ctx.gw.dues(Intent.PAY_NOW)
    amount = d["overdue_amount"] or d["emi_amount"]
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
    if ctx.answerer is not None:
        draft = ctx.answerer.answer(ctx.summary_text, facts)
        if draft:
            g = check(draft, verified_account=ctx.gw.scope.account_id, allowed_numbers=ctx.gw.allowed_numbers,
                      allowed_strings=ctx.gw.allowed_strings, constants={ctx.settings.helpline})
            if g.ok:
                return HandlerOut([Reply(text=draft, template_id="account_answer_llm")])
            ctx.gw._event(EventType.OUTPUT_BLOCKED, reason=g.reason, detail=g.detail, template_id="account_answer_llm")
    return HandlerOut([_facts_reply(ctx, facts)])


def handle_restricted(ctx: Ctx, task: Task) -> HandlerOut:
    ctx.cite("PRV-01")
    return HandlerOut([render("restricted_info", ctx.lang)])


def handle_policy_question(ctx: Ctx, task: Task) -> HandlerOut:
    """'How does approval work?', 'Will a settlement affect my credit score?': answered with the
    borrower lines of the retrieved policy clauses, word for word."""
    from servicing_agent.agent.policy import policy
    found = policy().retrieve(ctx.summary_text, k=2, with_borrower_line=True)
    if not found:
        return HandlerOut([render("clarify", ctx.lang)], resolution="clarify")
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
    res = ctx.gw.reconcile_claim(s.get("utr"), s.get("amount"), s.get("txn_date"), s.get("payee_vpa"))
    if res.verdict == Verdict.NEED_UTR:
        return HandlerOut([render("claim_need_utr", ctx.lang)], resolution="clarify")

    if res.verdict in (Verdict.MATCHED, Verdict.ALREADY_CONFIRMED):
        r = ctx.gw.receipt(res.txn["utr"])                 # same txn -> same document id
        ctx.gw.record_claim(res, s)
        ctx.gw.record_receipt(res.txn["txn_id"], r["document_id"])   # no-op if already issued
        ctx.claims_this_turn.append({"status": "matched", "amount": float(r["amount"])})
        ctx.cite("CLM-01")
        if recheck and res.verdict == Verdict.ALREADY_CONFIRMED:
            return HandlerOut([], resolution="auto")       # confirmed meanwhile (e.g. a resend): say nothing
        tpl = "claim_matched" if res.verdict == Verdict.MATCHED else "claim_already_confirmed"
        # TODO(parked): emit dunning.suppress for this account.
        return HandlerOut([render(tpl, ctx.lang, amount=money(r["amount"]), payment_date=r["payment_date"],
                                  utr=r["utr"], attachments=[r["document_id"]])])

    auth = ctx.gw.assess_claim(s, res)
    flagged = res.verdict in FLAGGED or auth.label == "likely_fake"
    detail = _claim_detail(s)
    prev = ctx.gw.previous_claim(s.get("utr"))
    already_ticketed = bool(prev and prev.get("ticket_id"))

    if flagged:
        ctx.cite("CLM-03", "CLM-04", "ESC-01")
        ctx.claims_this_turn.append({"status": "unverified"})
        tickets = []
        if already_ticketed:                               # same receipt again: no second ticket
            ctx.gw.record_claim(res, s)
        else:
            tickets = [_claim_ticket(ctx, res, s, auth, flagged=True)]
        # Neutral on purpose: the borrower is not told the receipt was flagged or escalated.
        return HandlerOut([] if recheck else [render("claim_unverified", ctx.lang)], tickets, "routed")

    if already_ticketed:
        ctx.gw.record_claim(res, s)
        ctx.claims_this_turn.append({"status": "escalated"})
        if recheck:
            return HandlerOut([], resolution="routed")
        return HandlerOut([render("claim_review_existing", ctx.lang, ticket_id=prev["ticket_id"])], resolution="routed")

    if not recheck:
        # Not in the ledger yet: give it the window, then look again.
        ctx.cite("CLM-01", "CLM-02")
        ctx.gw.record_claim(res, s)
        ctx.claims_this_turn.append({"status": "checking"})
        window = ctx.settings.claim_window_seconds
        scope = ctx.gw.scope
        follow = {"kind": "recheck_claim", "delay_seconds": window,
                  "account_id": scope.account_id, "customer_id": scope.customer_id,
                  "slots": {**s, "recheck": True, "first_seen_at": (ctx.received_at or datetime.utcnow())
                            .isoformat(timespec="seconds")}}
        return HandlerOut([render("claim_checking", ctx.lang, detail=detail, window=_window_text(window))],
                          resolution="pending", followups=[follow])

    # Recheck at the end of the window and still not in the ledger: failed, escalation filed.
    ctx.cite("CLM-02", "CLM-04", "ESC-01")
    t = _claim_ticket(ctx, res, s, auth, flagged=False)
    ctx.claims_this_turn.append({"status": "escalated"})
    return HandlerOut([render("claim_failed_escalated", ctx.lang, detail=detail, window=_window_text(
        ctx.settings.claim_window_seconds), ticket_id=t["ticket_id"])], [t], "routed")


def _window_text(seconds: int) -> str:
    if seconds >= 60:
        m = round(seconds / 60)
        return "1 minute" if m == 1 else f"{m} minutes"
    return "1 second" if seconds == 1 else f"{seconds} seconds"


def _claim_ticket(ctx: Ctx, res, s: dict, auth, *, flagged: bool) -> dict:
    summary = {"intent": "payment_claim", "verdict": res.verdict.value, "reasons": res.reasons,
               "source": s.get("source") or "text",
               "claimed": {k: s.get(k) for k in ("utr", "amount", "txn_date", "txn_time", "payee_vpa")},
               "authenticity": auth.as_dict(), "payment_age_minutes": payment_age_minutes(s, ctx.received_at),
               "first_seen_at": s.get("first_seen_at"), "rechecked": bool(s.get("recheck")),
               "flag": "SUSPECTED_MISMATCH" if flagged else None,
               "borrower_informed": not flagged, "triage": ctx.triage_info()}
    t = ctx.gw.ticket("payment_mismatch" if flagged else "payment_unmatched", summary)
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
    if not is_material_change(old, new):
        return HandlerOut([render("waiver_duplicate", ctx.lang, case_id=open_case.case_id)], resolution="routed")

    new["borrower_text"] = req.borrower_text
    pending = PendingChange(case_id=open_case.case_id, from_version=open_case.version, request=new)
    reply = render("waiver_confirm_change", ctx.lang, old_desc=_desc(old), new_desc=_desc(new), buttons=["Yes", "No"])
    return HandlerOut([reply], resolution="routed", pending_change=pending)


def _propose_case(ctx: Ctx, req: CaseRequest) -> HandlerOut:
    """Explain what a waiver / settlement involves and ask before submitting anything.

    With a payment screenshot in the same turn, the payment is checked against the ledger first:
    * not confirmed (not in the ledger yet, or couldn't be verified) -> no request yet
    * confirmed and it covers the full amount due -> nothing to waive, no request
    * confirmed but partial -> shortfall shown; on YES the request also goes to compliance
    """
    from servicing_agent.agent.policy import clauses_for_case, consequences
    kind = req.type.value
    partial = None
    if ctx.claims_this_turn:
        if any(c["status"] != "matched" for c in ctx.claims_this_turn):
            ctx.cite("WVR-02")
            return HandlerOut([render("case_waits_for_payment", ctx.lang, kind=kind)], resolution="pending")
        paid = round(sum(c["amount"] for c in ctx.claims_this_turn), 2)
        d = ctx.gw.dues(Intent.EMI_DUE_QUERY)
        due = float(d["overdue_amount"] or 0) or float(d["emi_amount"] or 0)
        if paid >= due - 0.5:
            ctx.cite("WVR-01")
            return HandlerOut([render("case_not_needed", ctx.lang, paid=money(paid), due=money(due),
                                      verb="waive" if kind == "waiver" else "settle")])
        shortfall = round(due - paid, 2)
        if req.amount is None:
            req = req.model_copy(update={"amount": shortfall})
        partial = {"paid": paid, "due": due, "shortfall": shortfall}
        ctx.gw.allow([paid, due, shortfall])
    ctx.gw.allow(req.amount)
    desc = _desc(req.model_dump(mode="json"))
    # What the borrower must hear comes from the policy document, not from this code.
    lines = consequences(kind, partial=bool(partial))
    bullets = "\n".join(f"• {c.borrower}" for c in lines)
    applied = [c.id for c in clauses_for_case(kind, partial=bool(partial))] + [c.id for c in lines]
    ctx.cite(*applied, "APR-03")
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
                                      "policy_refs": list(ctx.gw.policy_refs)})


def submit_case(ctx: Ctx, action: dict) -> HandlerOut:
    """The borrower said YES to submitting a waiver / settlement request."""
    req = CaseRequest(**action["request"])
    if ctx.gw.open_case() is not None:                    # submitted meanwhile (e.g. a double YES)
        return HandlerOut([render("waiver_duplicate", ctx.lang, case_id=ctx.gw.open_case().case_id)], resolution="routed")
    ctx.cite(*(action.get("policy_refs") or []), "APR-03", "ESC-01")
    case = ctx.gw.create_case(req)
    partial = action.get("partial_payment")
    summary = {"case_id": case.case_id, "version": case.version, "request": req.model_dump(mode="json"),
               "triage": action.get("triage") or ctx.triage_info(), "borrower_confirmed": True,
               "partial_payment": partial}
    tickets = [ctx.gw.ticket(f"{req.type.value}_request", summary)]
    if partial:
        # Paid only part of what's due and asking to waive / settle the rest: compliance is told now,
        # alongside the supervisor's approval, not only at step 2.
        tickets.append(ctx.gw.ticket("partial_payment_waiver", {**summary, "approval_ticket": tickets[0]["ticket_id"]}))
    return HandlerOut([render("waiver_created", ctx.lang, kind=req.type.value, case_id=case.case_id)], tickets, "routed")


def confirm_change(ctx: Ctx, pending: PendingChange, yes: bool) -> HandlerOut:
    if not yes:
        return HandlerOut([render("waiver_change_kept", ctx.lang, case_id=pending.case_id)], resolution="routed")
    case = ctx.gw.supersede_case(pending.case_id, pending.from_version, CaseRequest(**pending.request), ctx.message_id)
    return HandlerOut([render("waiver_restarted", ctx.lang, case_id=case.case_id)], resolution="routed")


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
