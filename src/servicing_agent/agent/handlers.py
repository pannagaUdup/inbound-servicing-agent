"""Intent handlers: code, not prompts. Each one calls tools only through the gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from servicing_agent.agent.gateway import PolicyGateway
from servicing_agent.agent.templates import STATUS_TEXT, TEAM_NAMES, money, render
from servicing_agent.config import Settings
from servicing_agent.contracts.cases import CaseRequest
from servicing_agent.contracts.state import HANDOFF_INTENTS, Intent, PendingChange, Reply, Task
from servicing_agent.agent.tools.reconciliation import FLAGGED, Verdict
from servicing_agent.platform.cases import is_material_change

Resolution = Literal["auto", "routed", "clarify", "pending"]


@dataclass
class HandlerOut:
    replies: list[Reply] = field(default_factory=list)
    tickets: list[dict] = field(default_factory=list)
    resolution: Resolution = "auto"
    pending_change: PendingChange | None = None


@dataclass
class Ctx:
    gw: PolicyGateway
    settings: Settings
    lang: str
    conversation_id: str
    message_id: str
    summary_text: str


def _route(ctx: Ctx, request_type: str, task: Task, handoff: bool = False) -> HandlerOut:
    summary = {"intent": task.intent.value, "borrower_message": ctx.summary_text[:500],
               "language": ctx.lang, "verified": ctx.gw.scope is not None, "slots": task.slots}
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


def handle_loan_summary(ctx: Ctx, task: Task) -> HandlerOut:
    s = ctx.gw.loan_summary()
    return HandlerOut([render("loan_summary", ctx.lang, product=str(s["product"]).replace("_", "-"),
                              principal=money(s["principal_outstanding"]), rate=f"{s['interest_rate']:.2f}",
                              emi=money(s["emi_amount"]), tenure_left=s["tenure_left"],
                              next_due_date=s["next_due_date"], overdue=money(s["overdue_amount"]))])


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
    """Every decided claim is written to the payment_claims ledger, whatever its source
    (screenshot, voice or typed text). Resending the same UTR is idempotent:

    * already confirmed for this borrower -> "already confirmed", same receipt, no ticket
    * already under review with the same verdict -> same ticket reference, no new ticket
    """
    s = task.slots
    res = ctx.gw.reconcile_claim(s.get("utr"), s.get("amount"), s.get("txn_date"), s.get("payee_vpa"))
    if res.verdict == Verdict.NEED_UTR:
        return HandlerOut([render("claim_need_utr", ctx.lang)], resolution="clarify")

    if res.verdict in (Verdict.MATCHED, Verdict.ALREADY_CONFIRMED):
        r = ctx.gw.receipt(res.txn["utr"])                 # same txn -> same document id
        ctx.gw.record_claim(res, s)
        ctx.gw.record_receipt(res.txn["txn_id"], r["document_id"])   # no-op if already issued
        tpl = "claim_matched" if res.verdict == Verdict.MATCHED else "claim_already_confirmed"
        # TODO(parked): emit dunning.suppress for this account.
        return HandlerOut([render(tpl, ctx.lang, amount=money(r["amount"]), payment_date=r["payment_date"],
                                  utr=r["utr"], attachments=[r["document_id"]])])

    if res.verdict == Verdict.PENDING:
        ctx.gw.record_claim(res, s)
        return HandlerOut([render("claim_pending", ctx.lang)], resolution="pending")

    prev = ctx.gw.previous_claim(s.get("utr"))
    if prev and prev.get("ticket_id") and prev.get("result") == res.verdict.value:
        # Same proof, same outcome: point to the existing review instead of opening another ticket.
        ctx.gw.record_claim(res, s)
        return HandlerOut([render("claim_review_existing", ctx.lang, ticket_id=prev["ticket_id"])],
                          resolution="routed")

    request_type = "payment_mismatch" if res.verdict in FLAGGED else "payment_unmatched"
    summary = {"intent": "payment_claim", "verdict": res.verdict.value, "reasons": res.reasons,
               "source": s.get("source") or "text",
               "claimed": {k: s.get(k) for k in ("utr", "amount", "txn_date", "payee_vpa")},
               "flag": "SUSPECTED_MISMATCH" if res.verdict in FLAGGED else None}
    t = ctx.gw.ticket(request_type, summary)
    ctx.gw.record_claim(res, s, ticket=t)
    # Never accuse the borrower: the message stays neutral, the internal flag is explicit.
    return HandlerOut([render("claim_review", ctx.lang)], [t], "routed")


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
        case = ctx.gw.create_case(req)
        t = ctx.gw.ticket(f"{req.type.value}_request", {"case_id": case.case_id, "version": case.version,
                                                        "request": req.model_dump(mode="json")})
        return HandlerOut([render("waiver_created", ctx.lang, case_id=case.case_id)], [t], "routed")

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
