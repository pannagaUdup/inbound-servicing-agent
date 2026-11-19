"""FastAPI surface (owner: T2).

uvicorn servicing_agent.platform.api.app:app --reload
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from servicing_agent.agent.graph import new_message_id
from servicing_agent.config import Settings, get_settings
from servicing_agent.contracts.cases import DecisionIn
from servicing_agent.contracts.state import InboundMessage, MediaEvidence, TurnResult
from servicing_agent.data.perception import extract_receipt, transcribe_voice_note
from servicing_agent.platform.cases import Conflict, NotFound
from servicing_agent.platform.context_pack import build_context_pack
from servicing_agent.platform.runtime import build_runtime


class SimulateIn(BaseModel):
    sender: str
    type: Literal["text", "image", "voice", "button"] = "text"
    text: str | None = None
    # Stand-in for perception until T3's STT/VLM lands: pre-extracted fields.
    receipt: dict | None = None          # {utr, amount, txn_date, payee_vpa, status, ocr_text}
    transcript: str | None = None        # voice-note transcript
    conversation_id: str | None = None
    message_id: str | None = None


def to_inbound(body: SimulateIn) -> InboundMessage:
    media: MediaEvidence | None = None
    if body.type == "image":
        media = extract_receipt(None, hint=body.receipt)
    elif body.type == "voice":
        media = transcribe_voice_note(None, hint_transcript=body.transcript)
    return InboundMessage(message_id=body.message_id or new_message_id(), sender=body.sender, type=body.type,
                          text=body.text, media=media)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Inbound WhatsApp Servicing Agent", version="0.1.0")
    runtime = build_runtime(settings)
    app.state.runtime = runtime

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "accounts": len(runtime.store.accounts), "planner": settings.planner}

    # ---- eval harness + demo
    @app.post("/v1/simulate/messages", response_model=TurnResult)
    def simulate(body: SimulateIn):
        return runtime.run_turn(to_inbound(body), body.conversation_id)

    # ---- WhatsApp BSP webhook (TODO(T2): HMAC signature check, enqueue, 200 immediately, worker pool)
    @app.get("/webhooks/whatsapp")
    def wa_verify(hub_mode: str = Query(None, alias="hub.mode"), hub_challenge: str = Query(None, alias="hub.challenge"),
                  hub_verify_token: str = Query(None, alias="hub.verify_token")):
        if hub_mode == "subscribe" and hub_challenge:
            return int(hub_challenge) if hub_challenge.isdigit() else hub_challenge
        raise HTTPException(403)

    @app.post("/webhooks/whatsapp")
    async def wa_inbound(request: Request):
        payload = await request.json()
        processed = 0
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for m in change.get("value", {}).get("messages", []):
                    if m.get("type") == "text":
                        runtime.run_turn(InboundMessage(message_id=m["id"], sender=m["from"], type="text",
                                                        text=m["text"]["body"]))
                        processed += 1
        return {"status": "accepted", "processed": processed}

    # ---- back office
    @app.get("/v1/conversations/{conversation_id}")
    def get_conversation(conversation_id: str):
        convo = runtime.conversations.get(conversation_id)
        if not convo:
            raise HTTPException(404)
        return {k: convo[k] for k in ("conversation_id", "transcript", "events", "tickets")} | {
            "verified": bool(convo["session"].get("scope"))}

    @app.get("/v1/tickets")
    def list_tickets(queue: str | None = None):
        return [t for t in runtime.tools.tickets.tickets if queue is None or t["queue"] == queue]

    # ---- waiver / settlement approvals
    @app.get("/v1/cases/{case_id}")
    def get_case(case_id: str):
        try:
            return runtime.cases.get(case_id)
        except NotFound:
            raise HTTPException(404)

    @app.get("/v1/cases/{case_id}/versions/{version}/context")
    def get_context(case_id: str, version: int):
        try:
            return build_context_pack(case_id, version, cases=runtime.cases, tools=runtime.tools,
                                      store=runtime.store, conversations=runtime.conversations)
        except (NotFound, StopIteration):
            raise HTTPException(404)

    @app.post("/v1/cases/{case_id}/versions/{version}/decisions")
    def decide(case_id: str, version: int, body: DecisionIn):
        try:
            return runtime.cases.decide(case_id, version, body)
        except Conflict as exc:
            raise HTTPException(409, detail=f"superseded or stale: {exc}")
        except NotFound:
            raise HTTPException(404)

    return app


def __getattr__(name: str):
    # Lazily build `app` so importing this module (e.g. in tests) doesn't need generated data.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
