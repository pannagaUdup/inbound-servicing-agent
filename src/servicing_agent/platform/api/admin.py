"""Admin panel (owner: T1). Phase 1.

Served by FastAPI (same origin) so the page can call the JSON endpoints and the DB
directly — unlike a static artifact. Covers:
  * Campaigns — select accounts, create, launch (mocked send), watch per-account journey.
  * Waivers  — the human reviewer console: pending cases, approve/reject (approver -> compliance).
  * Overview — a small manager strip (campaign funnel, open cases, tickets by queue),
               the seed of the WF-04 collection-operations view.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

from servicing_agent.agent.graph import new_message_id
from servicing_agent.config import REPO_ROOT
from servicing_agent.contracts.cases import OPEN_STATUSES, DecisionIn
from servicing_agent.contracts.state import InboundMessage
from servicing_agent.platform.cases import Conflict, NotFound
from servicing_agent.platform.db import Case, CaseVersion

WEB_DIR = REPO_ROOT / "web"


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
        tickets = getattr(runtime.tools.tickets, "tickets", [])
        by_queue: dict[str, int] = {}
        for t in tickets:
            by_queue[t.get("queue", "?")] = by_queue.get(t.get("queue", "?"), 0) + 1
        return {"campaigns": len(camps), "funnel": funnel,
                "open_cases": len(_pending_cases(runtime)), "tickets_by_queue": by_queue,
                "accounts": len(runtime.store.accounts)}

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
            return runtime.cases.decide(case_id, version, body)
        except Conflict as exc:
            raise HTTPException(409, detail=str(exc))
        except NotFound:
            raise HTTPException(404)

    app.include_router(r)


def _pending_cases(runtime) -> list[dict]:
    open_vals = [st.value for st in OPEN_STATUSES]
    with runtime.session_factory() as s:
        rows = s.execute(
            select(CaseVersion, Case).join(Case, Case.case_id == CaseVersion.case_id)
            .where(CaseVersion.version == Case.current_version, CaseVersion.status.in_(open_vals))
            .order_by(CaseVersion.created_at.desc())
        ).all()
        return [{"case_id": c.case_id, "account_id": c.account_id, "version": v.version,
                 "status": v.status, "type": v.type, "amount": float(v.amount) if v.amount is not None else None,
                 "etag": v.etag, "gaming_flag": c.gaming_flag,
                 "created_at": v.created_at.isoformat(timespec="seconds")} for v, c in rows]

