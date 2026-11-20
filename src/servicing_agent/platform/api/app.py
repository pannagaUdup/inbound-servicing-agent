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
from servicing_agent.platform.rechecks import RecheckScheduler
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

    def _deliver_text(sender: str, text: str) -> tuple[bool, str | None, str | None]:
        """Send one text; one retry on failure. Returns (ok, whatsapp message id, error)."""
        import time as _t
        err = None
        for attempt in (1, 2):
            try:
                resp = runtime.channel.send_text(sender, text) or {}
                wamid = ((resp.get("messages") or [{}])[0] or {}).get("id") if isinstance(resp, dict) else None
                return True, wamid, None
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:300]
                logger.warning("SEND failed to %s (attempt %d): %s", sender, attempt, err)
                if attempt == 1:
                    _t.sleep(1.0)
        return False, None, err

    def _send_replies(sender: str, replies, conversation_id: str | None = None, kind: str = "turn") -> None:
        """Send replies and record each delivery (ok / failed, WhatsApp id) in the audit log."""
        docs = getattr(runtime.tools, "docs", None)
        cid = conversation_id or runtime.conversation_id_for(sender)
        for reply in replies:
            if reply.text:
                ok, wamid, err = _deliver_text(sender, reply.text)
                logger.info("SENT %s template=%s to=%s ok=%s wamid=%s", kind, reply.template_id, sender, ok, wamid)
                if runtime.audit_sink:
                    try:
                        runtime.audit_sink(cid, [{"type": "delivery", "data": {
                            "template_id": reply.template_id, "kind": kind, "ok": ok, "wa_message_id": wamid,
                            "error": err}}])
                    except Exception:  # noqa: BLE001 - audit must not block delivery
                        logger.exception("delivery audit failed")
            for doc_id in getattr(reply, "attachments", []):
                try:
                    path = docs.path_for(doc_id) if docs and hasattr(docs, "path_for") else None
                    if path:
                        runtime.channel.send_document(sender, path, filename=f"{doc_id}.pdf")
                        logger.info("SENT document %s to %s", doc_id, sender)
                    else:
                        logger.warning("attachment %s has no PDF on disk; skipping", doc_id)
                except Exception:  # noqa: BLE001
                    logger.exception("failed to send document %s", doc_id)

    def _run_recheck(job: dict) -> None:
        res = runtime.recheck_claim(job["conversation_id"], job["slots"], job.get("account_id"),
                                    job.get("customer_id"))
        if res is None:
            logger.warning("RECHECK %s skipped: no scope", job["conversation_id"])
            return
        logger.info("RECHECK %s attempt %s -> %s", job["conversation_id"], job["slots"].get("recheck_attempt"),
                    [r.template_id for r in res.replies] or "still waiting")
        _send_replies(job["sender"], res.replies, job["conversation_id"], kind="recheck")
        _schedule_followups(job["sender"], res)

    rechecks = RecheckScheduler(_run_recheck)             # Redis attached at startup when available
    app.state.rechecks = rechecks

    def _schedule_followups(sender: str, result: TurnResult) -> None:
        for f in result.followups:
            if f.get("kind") == "recheck_claim":
                job = {"sender": sender, "conversation_id": result.conversation_id, "slots": f["slots"],
                       "account_id": f.get("account_id"), "customer_id": f.get("customer_id")}
                rechecks.schedule(job, f.get("delay_seconds", 120))
                logger.info("SCHEDULED claim recheck %s for %s in %ss (%s)", f["slots"].get("recheck_attempt"),
                            result.conversation_id, f.get("delay_seconds"),
                            "redis" if rechecks.durable else "in-process")

    def _run_agent(msg: InboundMessage) -> None:
        result = runtime.run_turn(msg)
        if result.duplicate:            # already handled this message id: never reply twice
            return
        # The question needs verification and there's no verified session (never verified, or it
        # expired). On WhatsApp, verify the same way as the menu: loan chooser / emailed code, then
        # answer the original question. Not the agent's own "share your account number".
        if (getattr(runtime, "inbound", None) is not None and not result.verified
                and any(r.template_id == "ask_account" for r in result.replies)):
            runtime.hand_verification_to_menu(result.conversation_id)
            _send_replies(msg.sender, [r for r in result.replies if r.template_id != "ask_account"])
            expired = any(e["type"] == "session_expired" for e in result.events)
            runtime.inbound.verify_for_message(msg, expired=expired)
            return
        _send_replies(msg.sender, result.replies, result.conversation_id)
        _schedule_followups(msg.sender, result)
        if getattr(runtime, "inbound", None) is not None and any(r.template_id == "claim_other_own_loan"
                                                                  for r in result.replies):
            runtime.inbound.remember_for_switch(msg)

    def _handle_one(msg: InboundMessage) -> None:
        cid = runtime.conversation_id_for(msg.sender)
        try:
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
                if inb.get("replay"):
                    # The question asked before verifying (text, screenshot or voice), answered now.
                    # New ids: the original ids are already marked processed (duplicate protection).
                    replay = InboundMessage(**{**inb["replay"], "message_id": new_message_id(), "batched_message_ids": []})
                    _run_agent(replay)
                return
            # 3) Plain agent turn.
            _run_agent(msg)
        finally:
            # Write the session through to the DB so a restart keeps verified scope + pending actions.
            runtime.conversations.persist(cid)

    MEDIA_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                 "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac",
                 "application/pdf": "pdf"}

    def _perceive(msg: InboundMessage) -> InboundMessage:
        """Worker-side: download the media from Meta by media_id, run perception, fill msg.media.

        Never leaves `media` empty: on any failure it carries an error, so the agent asks the
        borrower to resend (or type the UTR) instead of treating the message as text.
        """
        if msg.media is not None:
            return msg

        def failed(reason: str) -> InboundMessage:
            ev = MediaEvidence(kind="voice_note" if msg.type == "voice" else "other", confidence=0.0,
                               error=reason, source_message_id=msg.message_id)
            return msg.model_copy(update={"media": ev})

        if not msg.media_id:
            logger.info("MEDIA type=%s but no media_id (nothing to download)", msg.type)
            return failed("no_media_id")
        try:
            data, mime = runtime.channel.download_media(msg.media_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MEDIA download failed id=%s: %s", msg.media_id, exc)
            return failed("download_failed")
        ext = MEDIA_EXT.get((mime or msg.media_mime or "").split(";")[0].strip(), "bin")
        filename = f"{msg.media_id}.{ext}"
        cid = runtime.conversation_id_for(msg.sender)
        if perception is None:
            # SA_PERCEPTION=stub: keep the file for later, but nothing can be extracted.
            try:
                settings.media_dir.mkdir(parents=True, exist_ok=True)
                (settings.media_dir / filename).write_bytes(data)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MEDIA save failed: %s", exc)
            logger.info("MEDIA saved (perception off) id=%s type=%s bytes=%d", msg.media_id, msg.type, len(data))
            return failed("perception_disabled")
        try:
            if msg.type == "image":
                ev = perception.image(data, filename=filename, message_id=msg.message_id, conversation_id=cid)
            else:
                ev = perception.voice(data, filename=filename, message_id=msg.message_id, conversation_id=cid)
        except Exception as exc:  # noqa: BLE001
            logger.exception("MEDIA perception failed id=%s: %s", msg.media_id, exc)
            return failed("perception_failed")
        if not ev.source_message_id:
            ev = ev.model_copy(update={"source_message_id": msg.message_id})
        logger.info("MEDIA perceived id=%s type=%s kind=%s error=%s", msg.media_id, msg.type, ev.kind, ev.error)
        return msg.model_copy(update={"media": ev})

    def _run_group(group: list[InboundMessage]) -> None:
        """Text / image / voice sent together -> ONE turn: perceive media in parallel, then merge
        in send order. A turn with media goes straight to the agent (it verifies if needed and keeps
        the claim until then); text-only turns go through the campaign and menu flows first."""
        if not group:
            return
        media_msgs = [m for m in group if m.type in ("image", "voice")]
        if media_msgs:
            with ThreadPoolExecutor(max_workers=min(4, len(media_msgs))) as pool:
                done = {m.message_id: r for m, r in zip(media_msgs, pool.map(_perceive, media_msgs))}
            group = [done.get(m.message_id, m) for m in group]
        merged = InboundMessage.merge_batch(group)
        if merged.all_media():
            _run_agent(merged)
        else:
            _handle_one(merged)

    def process_batch(msgs: list[InboundMessage]) -> None:
        """One conversation's batched messages, in send order.

        - A button tap is handled on its own, as type 'button' (the menu and campaign flows need it).
        - Everything between taps (text, images, voice) is one merged turn, so "I paid" + screenshot
          + "pls check" gets one answer.
        """
        group: list[InboundMessage] = []
        for m in msgs:
            if m.type == "button":
                _run_group(group)
                group = []
                _handle_one(m)
            else:
                group.append(m)
        _run_group(group)

    def send_holding(sender: str) -> None:
        try:
            runtime.channel.send_text(sender, HOLDING)
        except Exception:  # noqa: BLE001
            pass

    queue = InboundQueue(settings.redis_url, process_batch=process_batch, send_holding=send_holding,
                         workers=settings.workers, batch_seconds=settings.batch_seconds,
                         recover_max_age_s=settings.queue_recover_max_age_s)
    app.state.queue = None
    app.state.process_batch = process_batch   # tests and the sync fallback call this directly

    @app.on_event("startup")
    async def _start_backbone():
        try:
            await queue.connect()
            await queue.start()
            app.state.queue = queue
        except Exception:  # noqa: BLE001 - Redis down -> synchronous fallback
            app.state.queue = None
        durable = RecheckScheduler.from_url(settings.redis_url, _run_recheck)
        rechecks.r = durable.r
        rechecks.start()

    @app.on_event("shutdown")
    async def _stop_backbone():
        if app.state.queue is not None:
            await queue.stop()
        rechecks.stop()

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
            result = runtime.run_turn(inbound, conversation_id)
            runtime.conversations.persist(conversation_id)
            return result

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
            result = runtime.run_turn(merged, body.conversation_id)
            runtime.conversations.persist(body.conversation_id)
            return result

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
            result = runtime.run_turn(merged, conversation_id)
            runtime.conversations.persist(conversation_id)
            return result

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
    def list_tickets(queue: str | None = None, status: str | None = None):
        return runtime.tools.tickets.list(queue=queue, status=status)

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
