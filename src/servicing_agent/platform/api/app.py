"""FastAPI surface (owner: T2).

uvicorn servicing_agent.platform.api.app:app --reload
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from servicing_agent.agent.graph import new_message_id
from servicing_agent.config import Settings, get_settings
from servicing_agent.contracts.cases import DecisionIn
from servicing_agent.contracts.state import InboundMessage, MediaEvidence, TurnResult
from servicing_agent.data.perception import extract_receipt, transcribe_voice_note
from servicing_agent.data.perception.media_store import MediaStore
from servicing_agent.data.perception.pipeline import Perception, PerceptionDisabled
from servicing_agent.platform.cases import Conflict, NotFound
from servicing_agent.platform.api.admin import register_admin
from servicing_agent.platform.channel import parse_inbound_webhook
from servicing_agent.platform.context_pack import build_context_pack
from servicing_agent.platform.queue import InboundQueue
from servicing_agent.platform.runtime import build_runtime

# Surface servicing_agent.* INFO logs through uvicorn (its default config hides app loggers).
_applog = logging.getLogger("servicing_agent")
if not _applog.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _applog.addHandler(_h)
    _applog.setLevel(logging.INFO)
    _applog.propagate = False
logger = logging.getLogger(__name__)


class SimulateIn(BaseModel):
    sender: str
    type: Literal["text", "image", "voice", "button"] = "text"
    text: str | None = None
    # Pre-extracted hints (stub perception; used by the eval harness).
    receipt: dict | None = None          # {utr, amount, txn_date, payee_vpa, status, ocr_text}
    transcript: str | None = None        # voice-note transcript
    # Real media (needs SA_PERCEPTION=sarvam): the file itself, base64-encoded.
    media_base64: str | None = None
    media_filename: str | None = None    # e.g. "receipt.png", "voice.ogg"
    conversation_id: str | None = None
    message_id: str | None = None

    @field_validator("sender")
    @classmethod
    def _sender_is_a_mobile_number(cls, v: str) -> str:
        if len(re.sub(r"\D", "", v)) < 10:
            raise ValueError("sender must be a mobile number with at least 10 digits, e.g. 919876543210")
        return v

    @field_validator("media_base64")
    @classmethod
    def _media_not_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("media_base64 is empty: the file was not read (check the path)")
        return v


class SimulateTurnIn(BaseModel):
    """A batch: messages from one borrower, in the order they were sent."""

    messages: list[SimulateIn] = Field(min_length=1, max_length=10)
    conversation_id: str | None = None


class BatchedTurnIn(BaseModel):
    """What the per-conversation batching worker sends after its 3 s window (T2 -> agent).

    Messages are InboundMessages in send order, with `media` already filled by perception.
    """

    conversation_id: str
    batched_at: datetime | None = None
    messages: list[InboundMessage] = Field(min_length=1, max_length=20)


def to_inbound(body: SimulateIn, perception: Perception | None = None,
               conversation_id: str = "unknown") -> InboundMessage:
    message_id = body.message_id or new_message_id()
    media: MediaEvidence | None = None
    if body.media_base64 and body.type in ("image", "voice"):
        if perception is None:
            raise PerceptionDisabled("Real media needs SA_PERCEPTION=sarvam and SARVAM_API_KEY")
        try:
            raw = base64.b64decode(body.media_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("media_base64 is not valid base64") from exc
        if body.type == "image":
            media = perception.image(raw, filename=body.media_filename or "receipt.png", message_id=message_id,
                                     conversation_id=conversation_id)
        else:
            media = perception.voice(raw, filename=body.media_filename or "voice.ogg", message_id=message_id,
                                     conversation_id=conversation_id)
    elif body.type == "image":
        media = extract_receipt(None, hint=body.receipt)
    elif body.type == "voice":
        media = transcribe_voice_note(None, hint_transcript=body.transcript)
    if media is not None and not media.source_message_id:
        media = media.model_copy(update={"source_message_id": message_id})   # links claims back to the file
    return InboundMessage(message_id=message_id, sender=body.sender, type=body.type, text=body.text, media=media)


def create_app(settings: Settings | None = None, perception: Perception | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Inbound WhatsApp Servicing Agent", version="0.1.0")
    runtime = build_runtime(settings)
    app.state.runtime = runtime
    perception = perception or (Perception.sarvam(store=MediaStore(settings.media_dir))
                                if settings.perception == "sarvam" else None)
    app.state.perception = perception
    if perception is not None and getattr(perception, "recorder", None) is None:
        perception.recorder = runtime.evidence           # media rows with the real file path + checksum
    register_admin(app, runtime)

    # ---- async backbone: ingress enqueues, worker pool processes ----
    HOLDING = "Thank you — we've received your message and are checking it now."

    def _run_agent(msg: InboundMessage) -> None:
        result = runtime.run_turn(msg)
        if result.duplicate:            # already handled this message id: never reply twice
            return
        for reply in result.replies:
            try:
                runtime.channel.send_text(msg.sender, reply.text)
            except Exception:  # noqa: BLE001 - don't fail on a send error
                pass

    def _handle_one(msg: InboundMessage) -> None:
        # 1) Outbound campaign journey (ack -> OTP -> verify).
        camp = runtime.campaigns.handle_reply(msg.sender, msg.text)
        if camp is not None and not camp.get("handoff_to_agent"):
            return
        # 2) Inbound borrower-initiated flow (greeting -> menu -> email OTP -> verify -> serve).
        inb = runtime.inbound.handle(msg)
        if inb is not None:
            for serve_text in inb.get("serve", []):
                _run_agent(InboundMessage(message_id=new_message_id(), sender=msg.sender,
                                          type="text", text=serve_text))
            return
        # 3) Plain agent turn.
        _run_agent(msg)

    def _fetch_media(msg: InboundMessage) -> None:
        """Worker-side: pull the actual media bytes from Meta by media_id, store them, and
        (TODO) hand to perception -> MediaEvidence. Downloading here keeps ingress fast."""
        if not msg.media_id:
            logger.info("MEDIA type=%s but no media_id (nothing to download)", msg.type)
            return
        try:
            data, mime = runtime.channel.download_media(msg.media_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MEDIA download failed id=%s: %s", msg.media_id, exc)
            return
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
               "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a"}.get(
                   (mime or msg.media_mime or "").split(";")[0].strip(), "bin")
        path = settings.media_dir / f"{msg.media_id}.{ext}"
        try:
            settings.media_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MEDIA save failed: %s", exc)
        logger.info("MEDIA extracted id=%s type=%s bytes=%d mime=%s saved=%s",
                    msg.media_id, msg.type, len(data), mime, path)
        # TODO(T3/LLM): perception.voice(data)/image(data) -> MediaEvidence -> msg.media

    def process_batch(msgs: list[InboundMessage]) -> None:
        """One conversation's batched messages, in order:
        - plain text bubbles merge into one turn
        - each button tap / image / voice stays its OWN turn (type preserved: a button
          must reach the handlers as type 'button', not merged into text)."""
        texts = [m for m in msgs if m.type == "text" and m.text]
        singles = [m for m in msgs if m.type in ("button", "image", "voice")]
        if texts:
            merged = InboundMessage(message_id=texts[-1].message_id or new_message_id(),
                                    sender=texts[0].sender, type="text",
                                    text=" ".join(t.text for t in texts if t.text))
            _handle_one(merged)
        for m in singles:
            if m.type in ("image", "voice"):
                _fetch_media(m)
            _handle_one(m)

    def send_holding(sender: str) -> None:
        try:
            runtime.channel.send_text(sender, HOLDING)
        except Exception:  # noqa: BLE001
            pass

    queue = InboundQueue(settings.redis_url, process_batch=process_batch, send_holding=send_holding,
                         workers=settings.workers, batch_seconds=settings.batch_seconds)
    app.state.queue = None

    @app.on_event("startup")
    async def _start_backbone():
        try:
            await queue.connect()
            await queue.start()
            app.state.queue = queue
        except Exception:  # noqa: BLE001 - Redis down -> synchronous fallback
            app.state.queue = None

    @app.on_event("shutdown")
    async def _stop_backbone():
        if app.state.queue is not None:
            await queue.stop()

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "accounts": len(runtime.store.accounts), "planner": settings.planner,
                "perception": settings.perception}

    # ---- eval harness + demo
    @app.post("/v1/simulate/messages", response_model=TurnResult)
    def simulate(body: SimulateIn):
        conversation_id = body.conversation_id or runtime.conversation_id_for(body.sender)
        # Hold the borrower's lock across perception AND the turn, so a slow screenshot can't be
        # overtaken by a text the borrower sent after it. Other borrowers are unaffected.
        with runtime.conversations.lock(conversation_id):
            try:
                inbound = to_inbound(body, perception, conversation_id)
            except PerceptionDisabled as exc:
                raise HTTPException(400, detail=str(exc))
            except ValueError as exc:
                raise HTTPException(422, detail=str(exc))
            return runtime.run_turn(inbound, conversation_id)

    @app.post("/v1/turns/batched", response_model=TurnResult)
    def batched_turn(body: BatchedTurnIn):
        """The batching worker's hand-off: one 3 s batch -> ONE agent turn.

        All bubbles (text, button, image, voice) are merged in send order, so "I have paid" +
        screenshot + "pls check" is one claim and one answer. Re-sending the same batch (same
        message ids) returns the original result with duplicate=true.
        """
        for m in body.messages:
            if runtime.conversation_id_for(m.sender) != body.conversation_id:
                raise HTTPException(422, detail=f"message {m.message_id}: sender does not match conversation_id")
            if m.type in ("image", "voice") and m.media is None:
                raise HTTPException(422, detail=f"message {m.message_id}: {m.type} has no media yet; "
                                                f"download media_id {m.media_id} and run perception first")
        with runtime.conversations.lock(body.conversation_id):
            merged = InboundMessage.merge_batch(body.messages)
            return runtime.run_turn(merged, body.conversation_id)

    @app.post("/v1/simulate/turn", response_model=TurnResult)
    def simulate_batch(body: SimulateTurnIn):
        """Several messages sent close together, handled as ONE turn (what the batching worker will do).

        Perception for every file runs in parallel; text and media are merged in send order.
        """
        senders = {runtime.conversation_id_for(m.sender) for m in body.messages}
        if len(senders) != 1:
            raise HTTPException(422, detail="all messages in a turn must come from the same sender")
        conversation_id = body.conversation_id or senders.pop()
        with runtime.conversations.lock(conversation_id):
            try:
                with ThreadPoolExecutor(max_workers=min(4, len(body.messages))) as pool:
                    inbound = list(pool.map(lambda m: to_inbound(m, perception, conversation_id), body.messages))
                merged = InboundMessage.merge_batch(inbound)
            except PerceptionDisabled as exc:
                raise HTTPException(400, detail=str(exc))
            except ValueError as exc:
                raise HTTPException(422, detail=str(exc))
            return runtime.run_turn(merged, conversation_id)

    # ---- WhatsApp Cloud API webhook (TODO: HMAC signature check, enqueue + worker pool for 500 concurrent)
    @app.get("/webhooks/whatsapp")
    def wa_verify(hub_mode: str = Query(None, alias="hub.mode"), hub_challenge: str = Query(None, alias="hub.challenge"),
                  hub_verify_token: str = Query(None, alias="hub.verify_token")):
        # Enforce the configured verify token; in dev (no token set) accept the handshake.
        if hub_mode == "subscribe" and hub_challenge and (
                not settings.wa_verify_token or hub_verify_token == settings.wa_verify_token):
            return int(hub_challenge) if hub_challenge.isdigit() else hub_challenge
        raise HTTPException(403)

    @app.post("/webhooks/whatsapp")
    async def wa_inbound(request: Request):
        # Ingress only: acknowledge + enqueue, return fast (<100 ms). Workers do the work.
        payload = await request.json()
        msgs = parse_inbound_webhook(payload)
        q = app.state.queue
        if q is not None:
            queued = 0
            for msg in msgs:
                if await q.enqueue(msg):   # dedupes WhatsApp retries by message id
                    queued += 1
            return {"status": "queued", "queued": queued}
        # Redis down: process synchronously so dev still works.
        for msg in msgs:
            process_batch([msg])
        return {"status": "accepted", "processed": len(msgs)}

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
