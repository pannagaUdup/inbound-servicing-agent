"""Admin panel (owner: T1). Phase 1.

Served by FastAPI (same origin) so the page can call the JSON endpoints and the DB
directly — unlike a static artifact. Covers:
  * Campaigns — select accounts, create, launch (mocked send), watch per-account journey.
  * Waivers  — the human reviewer console: pending cases, approve/reject (approver -> compliance).
  * Overview — a small manager strip (campaign funnel, open cases, tickets by queue),
               the seed of the WF-04 collection-operations view.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from servicing_agent.agent.graph import new_message_id
from servicing_agent.config import REPO_ROOT, policy_bands, routing_matrix
from servicing_agent.contracts.cases import OPEN_STATUSES, DecisionIn
from servicing_agent.contracts.state import InboundMessage
from servicing_agent.platform.cases import Conflict, NotFound
from servicing_agent.platform.db import AuditEvent, Case, CaseVersion, PaymentClaim, Ticket
from servicing_agent.platform.ticketing import QUEUE_GROUP

WEB_DIR = REPO_ROOT / "web"
logger = logging.getLogger(__name__)


def _notify_outcome(runtime, case_id: str, version: int, status: str) -> None:
    """Send the borrower the approval/rejection outcome and, for APPROVED, mark the case COMMUNICATED.

    Dev/sim sends free text; on a live channel this should be a Meta-approved template when outside
    the 24h window (free text is only valid in-session). Amounts come from the case record, so the
    message is grounded. Idempotent: communicate() only fires once (APPROVED -> COMMUNICATED)."""
    full = runtime.cases.get(case_id)
    ver = next((v for v in full["versions"] if v["version"] == version), full["versions"][-1])
    req = ver.get("request") or {}
    kind = req.get("type", "request")
    amount = req.get("amount")
    amt = f"₹{float(amount):,.2f}" if amount is not None else ""
    acc = full.get("account_id") or ""
    masked = f"XXXX{acc[-4:]}" if len(acc) >= 4 else acc
    cid = full.get("conversation_id") or ""
    mobile = cid.split(":", 1)[-1] if ":" in cid else cid
    if status == "APPROVED":
        # Revised amount to collect = overdue minus the concession, then a pay-link for exactly that.
        #   waiver     -> borrower pays (overdue − waived charges); the waiver covers the rest.
        #   settlement -> borrower pays the settled amount; the difference is written off.
        overdue = float((runtime.store.accounts.get(acc) or {}).get("overdue_amount") or 0)
        req_amt = float(amount or 0)
        payable = concession = None
        if kind == "waiver":
            concession = round(min(req_amt, overdue), 2)
            payable = round(overdue - concession, 2)
        elif kind == "settlement":
            payable = round(min(req_amt, overdue) if req_amt else overdue, 2)
            concession = round(overdue - payable, 2)
        link_url = None
        if payable is not None and payable > 0 and getattr(runtime, "paylinks", None):
            try:
                link_url = runtime.paylinks.create(acc, payable, cid, waived=concession or 0)["payment_link"]
            except Exception:  # noqa: BLE001 - a link failure must not break the decision
                logger.exception("pay-link create failed for approved case %s", case_id)
        if link_url:
            brk = f" (overdue ₹{overdue:,.2f} − {kind} ₹{concession:,.2f})" if concession else ""
            msg = (f"Good news — your {kind}{(' of ' + amt) if amt else ''} on loan {masked} is approved. "
                   f"Your revised amount payable is ₹{payable:,.2f}{brk}. Pay here: {link_url} — your "
                   f"account clears once this is paid. Thank you.")
        elif payable is not None and payable <= 0:
            msg = (f"Good news — your {kind}{(' of ' + amt) if amt else ''} on loan {masked} is approved and "
                   f"fully covers your dues — there's nothing further to pay. Thank you.")
        else:
            msg = (f"Good news — your {kind} request{(' of ' + amt) if amt else ''} on loan {masked} has been "
                   f"approved. Our team will apply it and share the updated terms shortly. Thank you.")
    else:  # REJECTED
        msg = (f"Update on your {kind} request{(' of ' + amt) if amt else ''} on loan {masked}: we're unable "
               f"to approve it at this time. Our team will reach out with the details and any alternatives.")
    if mobile:
        try:
            runtime.channel.send_text(mobile, msg)
        except Exception:  # noqa: BLE001 - a live-window/template failure must not break the decision
            logger.warning("outcome send failed for %s (likely outside 24h window; needs a template)", cid)
    if status == "APPROVED":
        runtime.cases.communicate(case_id, version)
    # Audit trail of the communication.
    try:
        from servicing_agent.platform.db import AuditEvent
        with runtime.session_factory.begin() as s:
            s.add(AuditEvent(conversation_id=cid, type="case_communicated",
                             data={"case_id": case_id, "version": version, "status": status, "text": msg}))
    except Exception:  # noqa: BLE001
        logger.exception("could not audit case_communicated for %s", case_id)


class CreateCampaignIn(BaseModel):
    name: str
    account_ids: list[str]
    message_body: str | None = None
    template_id: str | None = None
    template_lang: str = "en_US"
    template_params: list[str] | None = None
    created_by: str | None = "admin"


class SimulateReplyIn(BaseModel):
    sender: str
    text: str | None = None


class TestSendIn(BaseModel):
    to: str
    template_id: str | None = None
    language: str = "en_US"
    params: list[str] | None = None
    text: str | None = None


def register_admin(app, runtime) -> None:
    r = APIRouter(prefix="/admin", tags=["admin"])

    if WEB_DIR.is_dir():
        app.mount("/ui-static", StaticFiles(directory=str(WEB_DIR)), name="ui-static")

    @r.get("", response_class=HTMLResponse)
    @r.get("/", response_class=HTMLResponse)
    def admin_home():
        index = WEB_DIR / "index.html"
        if index.is_file():
            return index.read_text(encoding="utf-8")
        raise HTTPException(500, "web/index.html not found")

    @r.get("/api/overview")
    def overview():
        camps = runtime.campaigns.list()
        funnel: dict[str, int] = {}
        for c in camps:
            for st, n in runtime.campaigns.get(c["campaign_id"])["by_state"].items():
                funnel[st] = funnel.get(st, 0) + n
        tickets = runtime.tools.tickets.list(status="open")
        by_queue: dict[str, int] = {}
        for t in tickets:
            by_queue[t.get("queue", "?")] = by_queue.get(t.get("queue", "?"), 0) + 1
        # Pulse-lite real counts (guarded so an empty/missing table yields 0).
        try:
            with runtime.session_factory() as s:
                fake_proofs_blocked = s.execute(
                    select(func.count()).select_from(PaymentClaim).where(
                        or_(PaymentClaim.result.in_(["EDITED", "REUSED", "NOT_FOUND"]),
                            PaymentClaim.queue == "fraud_review"))
                ).scalar() or 0
        except Exception:  # noqa: BLE001
            fake_proofs_blocked = 0
        return {"campaigns": len(camps), "funnel": funnel,
                "open_cases": len(_pending_cases(runtime)), "open_tickets": len(tickets),
                "tickets_by_queue": by_queue, "accounts": len(runtime.store.accounts),
                "fake_proofs_blocked": fake_proofs_blocked,
                "shared_before_verification": 0}  # enforced invariant: nothing shown before OTP

    @r.get("/api/accounts")
    def accounts(product: str | None = None, dpd_min: int | None = None, q: str | None = None, limit: int = 100):
        return runtime.campaigns.selectable_accounts(product=product, dpd_min=dpd_min, q=q, limit=limit)

    @r.get("/api/campaigns")
    def list_campaigns():
        return runtime.campaigns.list()

    @r.post("/api/campaigns")
    def create_campaign(body: CreateCampaignIn):
        if not body.account_ids:
            raise HTTPException(400, "no accounts selected")
        return runtime.campaigns.create(body.name, body.account_ids, message_body=body.message_body,
                                        template_id=body.template_id, template_lang=body.template_lang,
                                        template_params=body.template_params, created_by=body.created_by)

    @r.get("/api/campaigns/{campaign_id}")
    def get_campaign(campaign_id: str):
        try:
            return runtime.campaigns.get(campaign_id)
        except KeyError:
            raise HTTPException(404)

    @r.post("/api/campaigns/{campaign_id}/launch")
    def launch_campaign(campaign_id: str):
        try:
            return runtime.campaigns.launch(campaign_id)
        except KeyError:
            raise HTTPException(404)

    @r.get("/api/campaigns/{campaign_id}/sim-targets")
    def sim_targets(campaign_id: str):
        """Targets with their (synthetic) mobile so the bot simulator can impersonate them. Dev only."""
        try:
            data = runtime.campaigns.get(campaign_id)
        except KeyError:
            raise HTTPException(404)
        out = []
        for t in data["targets"]:
            contact = runtime.store.contacts.get(t["contact_id"] or "")
            out.append({"account_id": t["account_id"], "state": t["state"],
                        "to": contact["mobile"] if contact else None})
        return out

    @r.post("/api/simulate")
    def simulate(body: SimulateReplyIn):
        """Drive the bot as if an inbound WhatsApp reply arrived: campaign journey first,
        then the agent. Returns the bot's outbound messages and the journey state."""
        camp = runtime.campaigns.handle_reply(body.sender, body.text)
        if camp is not None and not camp.get("handoff_to_agent"):
            return {"source": "campaign", "replies": camp["sent"], "state": camp["state"],
                    "account_id": camp["account_id"]}
        msg = InboundMessage(message_id=new_message_id(), sender=body.sender, type="text", text=body.text)
        res = runtime.run_turn(msg)
        runtime.conversations.persist(runtime.conversation_id_for(body.sender))
        return {"source": "agent", "replies": [rp.text for rp in res.replies], "verified": res.verified,
                "intents": [i.value for i in res.intents], "state": (camp or {}).get("state")}

    @r.get("/api/channel")
    def channel_status():
        ch = type(runtime.channel).__name__
        return {"channel": "whatsapp" if ch == "WhatsAppChannel" else "mock",
                "class": ch,
                "phone_number_id": runtime.settings.wa_phone_number_id or None,
                "api_version": runtime.settings.wa_api_version,
                "token_configured": bool(runtime.settings.whatsapp_token)}

    @r.post("/api/test-send")
    def test_send(body: TestSendIn):
        """Send one message straight through the active channel (real WhatsApp when configured)."""
        try:
            if body.template_id:
                res = runtime.channel.send_template(body.to, body.template_id, language=body.language,
                                                    params=body.params)
            else:
                res = runtime.channel.send_text(body.to, body.text or "Test message from CreditNirvana.")
            return {"ok": True, "channel": type(runtime.channel).__name__, "response": res}
        except Exception as exc:  # noqa: BLE001 - surface the Meta error to the admin
            raise HTTPException(502, detail=f"{type(exc).__name__}: {exc}")

    @r.get("/api/cases")
    def pending_cases():
        return _pending_cases(runtime)

    @r.post("/api/cases/{case_id}/versions/{version}/decisions")
    def decide(case_id: str, version: int, body: DecisionIn):
        try:
            out = runtime.cases.decide(case_id, version, body)
        except Conflict as exc:
            raise HTTPException(409, detail=str(exc))
        except NotFound:
            raise HTTPException(404)
        # Link the shadow approval ticket to the case lifecycle: once the case is terminal, close its
        # open tickets so the Handoff desk and the approval workflow don't diverge.
        if out.status.value in ("APPROVED", "REJECTED"):
            try:
                for t in runtime.tools.tickets.list(status="open"):
                    if (t.get("summary") or {}).get("case_id") == case_id:
                        runtime.tools.tickets.resolve(t["ticket_id"])
            except Exception:  # noqa: BLE001 - never fail the decision on a ticket cleanup
                pass
            # Close the loop: tell the borrower the outcome, then move APPROVED -> COMMUNICATED.
            try:
                _notify_outcome(runtime, case_id, version, out.status.value)
            except Exception:  # noqa: BLE001 - never fail the decision on a notify error
                logger.exception("outcome notification failed for %s", case_id)
        return out

    @r.get("/api/cases/stats")
    def case_stats():
        """Counts by status across all cases (incl. terminal), for the Approvals states row."""
        try:
            return runtime.cases.status_counts()
        except Exception:  # noqa: BLE001
            return {}

    @r.get("/api/tickets")
    def list_tickets(queue: str | None = None, status: str | None = "open", limit: int = 200):
        """The ops console: routed requests (handoffs, disputes, unmatched payments, ...),
        enriched with the borrower's name. Everything is assigned to one ops user for now."""
        rows = runtime.tools.tickets.list(queue=queue, status=status, limit=limit)
        for t in rows:
            t["borrower_name"] = _borrower_name(runtime, t.get("account_id"))
        return rows

    @r.post("/api/tickets/{ticket_id}/resolve")
    def resolve_ticket(ticket_id: str):
        if not runtime.tools.tickets.resolve(ticket_id):
            raise HTTPException(404, detail="ticket not found or already resolved")
        return {"ticket_id": ticket_id, "status": "resolved"}

    @r.get("/api/metrics/pulse")
    def metrics_pulse(days: int = 29):
        return runtime.metrics.pulse(days)

    @r.get("/api/metrics/journeys")
    def metrics_journeys():
        return runtime.metrics.journeys()

    @r.get("/api/config")
    def config_facts():
        """Routing & escalation matrix + waiver/settlement policy bands (Guardrails view)."""
        return {"routing_matrix": routing_matrix(), "policy_bands": policy_bands()}

    @r.get("/api/metrics/guardrails")
    def metrics_guardrails(days: int = 29):
        return runtime.metrics.guardrails(days)

    @r.get("/api/guardrails/events")
    def guardrail_events(limit: int = 30):
        """Recent hard-guardrail actions: prompt-injection blocks, output-guard blocks, security
        alerts, and fake/edited payment proofs — the live feed behind the counters."""
        out = []
        try:
            with runtime.session_factory() as s:
                for typ, at, data in s.execute(
                    select(AuditEvent.type, AuditEvent.at, AuditEvent.data)
                    .where(AuditEvent.type.in_(["injection_detected", "output_blocked", "security_alert"]))
                    .order_by(AuditEvent.at.desc()).limit(limit)).all():
                    d = data or {}
                    out.append({"type": typ, "at": at.isoformat(timespec="seconds"),
                                "detail": d.get("reason") or d.get("detail") or d.get("intent") or ""})
                for res, ct, q in s.execute(
                    select(PaymentClaim.result, PaymentClaim.created_at, PaymentClaim.queue)
                    .where(or_(PaymentClaim.result.in_(["EDITED", "REUSED", "NOT_FOUND"]),
                               PaymentClaim.queue == "fraud_review"))
                    .order_by(PaymentClaim.created_at.desc()).limit(limit)).all():
                    out.append({"type": "fake_proof_blocked", "at": ct.isoformat(timespec="seconds"),
                                "detail": (res or "") + (" → " + q if q else "")})
        except Exception:  # noqa: BLE001
            logger.exception("guardrail events query failed")
        out.sort(key=lambda x: x["at"], reverse=True)
        return out[:limit]

    @r.post("/api/eval/run")
    def eval_run(n: int = 20, seed: int = 7):
        """Run the labeled scenario grid through the real agent and return outcome-vs-label.
        Capped for the UI; the full gate is `python -m evals.run_eval`."""
        import sys
        if str(REPO_ROOT) not in sys.path:   # evals/ is a repo-root package, not under src/
            sys.path.insert(0, str(REPO_ROOT))
        from evals.run_eval import run as _run  # lazy: run_eval imports app.py (circular at module load)
        n = max(1, min(n, 80))
        summary, results = _run(str(runtime.settings.data_dir), n, seed)
        return {"summary": summary, "results": results}

    @r.get("/api/agencies")
    def list_agencies():
        return runtime.agencies.list_agencies()

    @r.get("/api/complaints")
    def list_complaints(status: str | None = "open"):
        return runtime.agencies.complaints(status=status)

    @r.get("/api/media/{media_id}")
    def get_media(media_id: str):
        """Serve a downloaded voice note / screenshot so the Conversations screen can play/show it.
        Perception-off saves media_dir/{media_id}.{ext}; the perception worker (Sarvam) saves it under
        a dated folder keyed by the message id, recording the media id as ``original_filename`` in the
        sidecar .json — so we resolve by that too."""
        import re as _re
        from fastapi.responses import FileResponse
        if not _re.fullmatch(r"[A-Za-z0-9_=.:-]{1,256}", media_id):   # no path traversal
            raise HTTPException(400, detail="bad media id")
        path = _resolve_media(runtime.settings.media_dir, media_id)
        if not path:
            raise HTTPException(404, detail="media not downloaded")
        return FileResponse(path)

    @r.get("/api/conversations")
    def list_conversations(limit: int = 200):
        """Conversations for the Conversations screen. Each row carries enough (languages, intents,
        assessment) for client-side search + filtering; the full detail of one is served by
        GET /admin/api/conversations/{id}."""
        out = []
        for convo in runtime.conversations.all():
            tr = convo.get("transcript", [])
            last = tr[-1] if tr else {}
            cid = convo.get("conversation_id") or ""
            scope = convo.get("session", {}).get("scope")
            out.append({
                "conversation_id": cid,
                "sender": cid.split(":", 1)[-1] if ":" in cid else cid,
                "name": _borrower_name(runtime, _scope_acct(scope)),
                "verified": bool(scope),
                "messages": len(tr),
                "tickets": len(convo.get("tickets", [])),
                "languages": _conv_langs(convo),
                "intents": _conv_intents(convo),
                "outcome": _conv_outcome(convo),
                "last_role": last.get("role"),
                "last_text": last.get("text"),
                "last_at": last.get("at"),
            })
        out.sort(key=lambda c: c.get("last_at") or "", reverse=True)
        return out[:limit]

    @r.get("/api/conversations/{cid}")
    def conversation_detail(cid: str):
        """Everything the Conversations side panel shows for one conversation: borrower profile,
        payment claims, linked tickets & cases, transcript, agent trace, and an auto assessment."""
        convo = runtime.conversations.get(cid)
        if not convo:
            raise HTTPException(404)
        scope = convo.get("session", {}).get("scope")
        acct = _scope_acct(scope)
        profile = None
        if acct:
            acc = runtime.store.accounts.get(acct)
            if acc:
                profile = {"account_id": acct, "name": _borrower_name(runtime, acct),
                           "product": acc.get("product"), "dpd": acc.get("dpd"),
                           "emi_amount": acc.get("emi_amount"), "overdue_amount": acc.get("overdue_amount"),
                           "principal_outstanding": acc.get("principal_outstanding"),
                           "next_due_date": acc.get("next_due_date"), "status": acc.get("status")}
        payments, cases, tickets = [], [], []
        try:
            with runtime.session_factory() as s:
                for utr, amount, result, queue, src, ct in s.execute(
                    select(PaymentClaim.utr, PaymentClaim.amount, PaymentClaim.result, PaymentClaim.queue,
                           PaymentClaim.source, PaymentClaim.created_at)
                    .where(PaymentClaim.conversation_id == cid)
                    .order_by(PaymentClaim.created_at.desc())).all():
                    payments.append({"utr": utr, "amount": float(amount) if amount is not None else None,
                                     "result": result, "queue": queue, "source": src,
                                     "at": ct.isoformat(timespec="seconds") if ct else None})
                for c, v in s.execute(
                    select(Case, CaseVersion).join(
                        CaseVersion, (CaseVersion.case_id == Case.case_id) & (CaseVersion.version == Case.current_version))
                    .where(Case.conversation_id == cid)).all():
                    cases.append({"case_id": c.case_id, "status": v.status, "type": v.type,
                                  "amount": float(v.amount) if v.amount is not None else None})
                for t in s.execute(select(Ticket).where(Ticket.conversation_id == cid)
                                   .order_by(Ticket.created_at.desc())).scalars().all():
                    tickets.append({"ticket_id": t.ticket_id, "request_type": t.request_type,
                                    "queue": t.routing_queue, "group": QUEUE_GROUP.get(t.routing_queue, "General"),
                                    "priority": t.priority, "status": t.status})
        except Exception:  # noqa: BLE001
            logger.exception("conversation detail query failed for %s", cid)
        return {"conversation_id": cid, "verified": bool(scope), "scope": acct, "profile": profile,
                "transcript": convo.get("transcript", []), "events": convo.get("events", []),
                "payments": payments, "cases": cases, "tickets": tickets,
                "outcome": _conv_outcome(convo)}

    from servicing_agent.platform.api.traces import register_traces
    register_traces(r, runtime)

    app.include_router(r)


_MEDIA_INDEX: dict[str, str] = {}   # media_id -> absolute file path (cached; rebuilt on a miss)


def _resolve_media(media_dir, media_id: str) -> str | None:
    """Find the saved file for a WhatsApp media id, however it was stored."""
    import glob
    import json
    import os
    root = str(media_dir)
    # 1) perception-off / seeded layout: <media_dir>/{media_id}.{ext}
    for p in glob.glob(os.path.join(root, glob.escape(media_id) + ".*")):
        if not p.endswith(".json"):
            return p
    # 2) cached hit from a previous sidecar scan (verify it still exists)
    cached = _MEDIA_INDEX.get(media_id)
    if cached and os.path.exists(cached):
        return cached
    # 3) scan the perception worker's dated sidecars: original_filename == {media_id}.{ext}
    for jp in glob.glob(os.path.join(root, "**", "*.json"), recursive=True):
        try:
            orig = (json.load(open(jp, encoding="utf-8")).get("original_filename") or "")
        except Exception:  # noqa: BLE001
            continue
        mid = os.path.splitext(orig)[0]
        if not mid:
            continue
        for cand in glob.glob(glob.escape(jp[:-5]) + ".*"):   # sibling, same stem, non-json
            if not cand.endswith(".json"):
                _MEDIA_INDEX[mid] = cand
                break
    cached = _MEDIA_INDEX.get(media_id)
    return cached if cached and os.path.exists(cached) else None


def _scope_acct(scope) -> str | None:
    """A conversation's verified account id. The live agent stores scope as a dict
    ({account_id, ...}); seeded/simple sessions store the account id as a plain string."""
    if isinstance(scope, dict):
        return scope.get("account_id")
    return scope if isinstance(scope, str) else None


def _conv_langs(convo: dict) -> list[str]:
    out: list[str] = []
    for e in convo.get("events", []):
        lang = (e.get("data") or {}).get("language")
        if lang and lang != "en" and lang not in out:
            out.append(lang)
    return out


def _conv_intents(convo: dict) -> list[str]:
    out: list[str] = []
    for e in convo.get("events", []):
        it = (e.get("data") or {}).get("intent")
        if it and it not in out:
            out.append(it)
    return out


def _conv_outcome(convo: dict) -> dict:
    """An auto assessment of how the conversation was handled (the 'evaluation' label)."""
    types = [e.get("type") for e in convo.get("events", [])]
    verdicts = [(e.get("data") or {}).get("verdict") for e in convo.get("events", [])
                if e.get("type") == "payment_verdict"]
    verified = bool(convo.get("session", {}).get("scope"))
    has_handoff = bool(convo.get("tickets")) or "ticket_created" in types or "handoff" in types
    if any(v in ("EDITED", "REUSED", "NOT_FOUND") for v in verdicts):
        return {"label": "Fraud / claim flagged", "cls": "bad"}
    if has_handoff:
        return {"label": "Escalated to human", "cls": "warn"}
    if any(t in ("verification_failed",) for t in types) and not verified:
        return {"label": "Verification failed", "cls": "bad"}
    if verified:
        return {"label": "Zero-touch resolved", "cls": "ok"}
    return {"label": "Guest / unverified", "cls": "muted"}


def _borrower_name(runtime, account_id: str | None) -> str | None:
    if not account_id:
        return None
    acc = runtime.store.accounts.get(account_id)
    if not acc:
        return None
    cust = runtime.store.customers.get(acc.get("customer_id", ""))
    return cust.get("name") if cust else None


def _pending_cases(runtime) -> list[dict]:
    open_vals = [st.value for st in OPEN_STATUSES]
    with runtime.session_factory() as s:
        rows = s.execute(
            select(CaseVersion, Case).join(Case, Case.case_id == CaseVersion.case_id)
            .where(CaseVersion.version == Case.current_version, CaseVersion.status.in_(open_vals))
            .order_by(CaseVersion.created_at.desc())
        ).all()
        out = []
        for v, c in rows:
            try:
                elig = runtime.cases._eligibility(c.account_id, v.request)
            except Exception:  # noqa: BLE001
                elig = None
            out.append({"case_id": c.case_id, "account_id": c.account_id, "version": v.version,
                        "status": v.status, "type": v.type,
                        "amount": float(v.amount) if v.amount is not None else None,
                        "etag": v.etag, "gaming_flag": c.gaming_flag, "assignee": c.assignee,
                        "eligibility": elig,
                        "created_at": v.created_at.isoformat(timespec="seconds")})
        return out

