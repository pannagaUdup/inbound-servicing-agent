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
from fastapi.responses import HTMLResponse
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


def _mask_acct(account_id: str) -> str:
    return account_id if len(account_id) <= 8 else f"{account_id[:4]}••••{account_id[-4:]}"


_PAY_CONSOLE_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Mock payments · CreditNirvana</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,Segoe UI,Roboto,system-ui,sans-serif;background:#0a1124;color:#eaf0fb;padding:24px}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:20px;margin:0 0 2px}.lede{color:#8d97ad;font-size:13px;margin:0 0 20px}
.card{background:#111a33;border:1px solid #24304d;border-radius:14px;padding:16px 18px;margin-bottom:16px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:#8d97ad;margin:0 0 12px}
table{width:100%;border-collapse:collapse}
td{padding:10px 8px;border-bottom:1px solid #1a2440;vertical-align:middle}
tr:last-child td{border-bottom:0}
.sub{color:#8d97ad;font-size:12px}
.amt{font-weight:700}
button{border:0;border-radius:9px;background:#1a56db;color:#fff;font-weight:700;font-size:14px;padding:8px 16px;cursor:pointer}
button:hover{background:#1546b8}button:disabled{opacity:.5;cursor:default}
.empty{color:#8d97ad;text-align:center;padding:22px}
form{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input{flex:1;min-width:140px;background:#0a1124;border:1px solid #24304d;border-radius:9px;color:#eaf0fb;padding:9px 11px;font:inherit}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#12875c;color:#fff;padding:10px 16px;border-radius:10px;font-weight:600;opacity:0;transition:opacity .2s;pointer-events:none}
#toast.show{opacity:1}#toast.err{background:#d93b30}
</style></head><body><div class="wrap">
<h1>Mock payments</h1>
<p class="lede">Simulate real-time borrower payments. Pending links (sent by the bot via “pay now”) appear here automatically — click Pay to mock the payment: it deducts the amount from overdue, mints a UTR + receipt, and confirms on WhatsApp.</p>
<div class="card"><h2>Pending payment links</h2><table><tbody id="rows"><tr><td class="empty">Loading…</td></tr></tbody></table></div>
<div id="toast"></div>
<script>
const money = v => "\\u20b9" + Number(v||0).toLocaleString("en-IN",{minimumFractionDigits:2});
let tmr;
function toast(msg, err){const t=document.getElementById("toast");t.textContent=msg;t.className="show"+(err?" err":"");clearTimeout(tmr);tmr=setTimeout(()=>t.className="",2600);}
async function load(){
  try{
    const list = await (await fetch("/pay/pending")).json();
    const tb = document.getElementById("rows");
    tb.innerHTML = list.length ? list.map(l=>`<tr>
      <td><b>${l.name||"—"}</b><div class="sub">${l.account_id}${l.conversation_id?" · "+l.conversation_id:""}</div></td>
      <td class="amt">${money(l.amount)}</td>
      <td class="sub">overdue ${money(l.overdue)}</td>
      <td style="text-align:right"><button data-id="${l.link_id}" onclick="pay(this)">Pay now</button></td></tr>`).join("")
      : `<tr><td class="empty">No pending links. Send a payment link from a conversation and it appears here.</td></tr>`;
  }catch(e){}
}
async function pay(btn){
  btn.disabled=true;btn.textContent="…";
  try{const r=await fetch("/pay/"+btn.dataset.id+"/confirm",{method:"POST"});toast(r.ok?"Payment done \\u2713":"Payment failed",!r.ok);}
  catch(e){toast("Payment failed",true);}
  load();
}
setInterval(load,4000);load();
</script></div></body></html>"""


def _pay_html(title: str, body: str) -> str:
    """A tiny self-contained mock checkout page (no external assets)."""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · CreditNirvana</title><style>
:root{{color-scheme:light dark}}
*{{box-sizing:border-box}}
body{{margin:0;font:16px/1.5 -apple-system,Segoe UI,Roboto,system-ui,sans-serif;background:#0b1f4b;
color:#0f1d3a;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px}}
.card{{background:#fff;border-radius:18px;max-width:380px;width:100%;padding:28px 26px;text-align:center;
box-shadow:0 20px 60px rgba(0,0,0,.35)}}
.brand{{font-weight:800;letter-spacing:-.02em;color:#0b1f4b;font-size:18px;margin-bottom:4px}}
h1{{font-size:16px;color:#64708a;font-weight:600;margin:0 0 18px}}
.amt{{font-size:40px;font-weight:800;color:#0b1f4b;margin:8px 0 2px}}
.acct{{color:#64708a;font-size:14px;margin:0 0 20px}}
.ok{{font-size:20px;font-weight:800;color:#12875c;margin:6px 0}}
button{{width:100%;border:0;border-radius:12px;background:#1a56db;color:#fff;font-size:17px;font-weight:700;
padding:14px;cursor:pointer}}button:hover{{background:#1546b8}}
.note{{color:#8a93a6;font-size:12px;margin-top:18px}}
</style></head><body><div class="card"><div class="brand">CreditNirvana</div>
<h1>{title}</h1>{body}</div></body></html>"""
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
    # Run follow-ups like the WhatsApp path does (e.g. the payment-claim recheck after the window). The
    # recheck's reply goes out on the real channel to `sender`. Off by default so evals stay synchronous.
    followups: bool = False

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

    from servicing_agent.platform.outbound import SendLedger, classify_failure, send_key
    ledger = getattr(runtime, "send_ledger", None) or SendLedger.from_url(
        None if settings.database_url.startswith("sqlite") else settings.redis_url)
    runtime.send_ledger = ledger

    def _deliver_text(sender: str, text: str) -> tuple[str, str | None, str | None]:
        """Send one text. Retries once only when it certainly wasn't delivered.
        Returns (status: sent|failed|unknown, whatsapp message id, error)."""
        import time as _t
        err, status = None, "failed"
        for attempt in (1, 2):
            try:
                resp = runtime.channel.send_text(sender, text) or {}
                wamid = ((resp.get("messages") or [{}])[0] or {}).get("id") if isinstance(resp, dict) else None
                return "sent", wamid, None
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:300]
                status = classify_failure(exc)
                logger.warning("SEND %s to %s (attempt %d): %s", status, sender, attempt, err)
                if status != "retry" or attempt == 2:
                    break
                _t.sleep(1.0)
        return ("unknown" if status == "unknown" else "failed"), None, err

    def _send_replies(sender: str, replies, conversation_id: str | None = None, kind: str = "turn",
                      turn_id: str | None = None) -> None:
        """Send replies at most once each (send ledger) and record each delivery in the audit log."""
        docs = getattr(runtime.tools, "docs", None)
        cid = conversation_id or runtime.conversation_id_for(sender)
        for i, reply in enumerate(replies):
            key = send_key(cid, turn_id, i, reply.text) if turn_id else None
            if key and not ledger.claim(key):
                logger.info("SKIP %s template=%s to=%s: already sent", kind, reply.template_id, sender)
                continue
            if reply.text:
                status, wamid, err = _deliver_text(sender, reply.text)
                ok = status == "sent"
                if key and status == "failed":
                    ledger.release(key)                       # certainly not delivered: a rerun may send it
                logger.info("SENT %s template=%s to=%s ok=%s status=%s wamid=%s", kind, reply.template_id, sender,
                            ok, status, wamid)
                if runtime.audit_sink:
                    try:
                        runtime.audit_sink(cid, [{"type": "delivery", "data": {
                            "template_id": reply.template_id, "kind": kind, "ok": ok, "status": status,
                            "wa_message_id": wamid, "error": err}}])
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
        sl = job["slots"]
        _send_replies(job["sender"], res.replies, job["conversation_id"], kind="recheck",
                      turn_id=f"recheck:{sl.get('utr')}:{sl.get('first_seen_at')}:{sl.get('recheck_attempt')}")
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
        if result.duplicate:
            # Already handled (webhook redelivery, queue recovery): send only replies that didn't go out.
            _send_replies(msg.sender, [r for r in result.replies if r.template_id != "ask_account"],
                          result.conversation_id, turn_id=msg.message_id)
            return
        # The question needs verification and there's no verified session (never verified, or it
        # expired). On WhatsApp, verify the same way as the menu: loan chooser / emailed code, then
        # answer the original question. Not the agent's own "share your account number".
        if (getattr(runtime, "inbound", None) is not None and not result.verified
                and any(r.template_id == "ask_account" for r in result.replies)):
            runtime.hand_verification_to_menu(result.conversation_id)
            _send_replies(msg.sender, [r for r in result.replies if r.template_id != "ask_account"],
                          turn_id=msg.message_id)
            expired = any(e["type"] == "session_expired" for e in result.events)
            runtime.inbound.verify_for_message(msg, expired=expired)
            return
        _send_replies(msg.sender, result.replies, result.conversation_id, turn_id=msg.message_id)
        _schedule_followups(msg.sender, result)
        if getattr(runtime, "inbound", None) is not None and any(r.template_id == "claim_other_own_loan"
                                                                  for r in result.replies):
            runtime.inbound.remember_for_switch(msg)

    REMINDER_BUTTONS = {"REMIND_PAY": None, "REMIND_PAID": "I have already paid this amount",
                        "REMIND_CANT": "I can't pay the full amount right now"}

    def _reminder_button(msg: InboundMessage, cid: str) -> None:
        from servicing_agent.agent.templates import render
        convo = runtime.conversations.load(cid)
        sess = convo["session"]
        rem = sess.get("reminder")
        if not rem:
            runtime.channel.send_text(msg.sender, "That reminder is no longer active. Say hi to see your options.")
            return
        account_id = rem["account_id"]
        if msg.text == "REMIND_PAY":                     # level 0: paying into the loan needs no code
            link = runtime.tools.paylinks.create(account_id, rem["amount"], cid)
            reply = render("reminder_pay", "en", amount=f"{float(rem['amount']):,.2f}", payment_link=link["payment_link"])
            _send_replies(msg.sender, [reply], cid, kind="reminder", turn_id=msg.message_id)
            return
        text = REMINDER_BUTTONS[msg.text]
        follow = InboundMessage(message_id=new_message_id(), sender=msg.sender, type="text", text=text)
        scope = sess.get("scope") or {}
        if scope.get("account_id") == account_id:      # already verified for this loan: continue straight away
            _run_agent(follow)
            return
        sess["inbound_replay"] = follow.model_dump(mode="json")   # answered right after the code
        sess["reminder"] = {**rem, "pending_button": msg.text}       # so the answer can refer back to it
        runtime.inbound._start_otp(msg.sender, sess, None, account_id)
        runtime.conversations.persist(cid)

    def _handle_one(msg: InboundMessage) -> None:
        cid = runtime.conversation_id_for(msg.sender)
        try:
            # "hi" / the menu starts the conversation over: the hardship ladder starts again for the loan.
            from servicing_agent.platform.inbound import _GREETING
            if msg.text and _GREETING.search(msg.text) and msg.type != "button":
                scope = (runtime.conversations.load(cid)["session"].get("scope") or {})
                if scope.get("account_id"):
                    from servicing_agent.agent.handlers import reset_ladder
                    reset_ladder(runtime.session_context, scope["account_id"])
            # Reminder buttons: Pay now needs no code; Already paid / Can't pay verify for the reminder's loan
            # (code only, no account number), then continue as if they had typed it.
            if msg.type == "button" and msg.text in REMINDER_BUTTONS:
                _reminder_button(msg, cid)
                return
            # 0) "logout" ends the session whatever stage they're in (menu, code, verified).
            from servicing_agent.agent.graph import LOGOUT_RE
            if msg.text and LOGOUT_RE.match(msg.text):
                res = runtime.run_turn(msg)
                _send_replies(msg.sender, res.replies, res.conversation_id, turn_id=msg.message_id)
                return
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
                rem = runtime.conversations.load(cid)["session"].get("reminder") or {}
                if inb.get("replay") and rem.get("pending_button"):
                    # They tapped a reminder button before logging in: say what we're continuing with.
                    from servicing_agent.agent.graph import PolicyGateway
                    from servicing_agent.agent.templates import render
                    _send_replies(msg.sender, [render("reminder_resume", "en", amount=f"{float(rem['amount']):,.2f}",
                                                      account=PolicyGateway.mask_account(rem["account_id"]))],
                                  cid, kind="reminder", turn_id=f"resume:{msg.message_id}")
                    rem.pop("pending_button", None)
                    runtime.conversations.load(cid)["session"]["reminder"] = rem
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

    # ---- ledger watcher: payments that reach the ledger after a borrower's claim (scenario 1.1, incl. late)
    seen_utrs: set[str] = set(runtime.store.payments_by_utr)

    def ledger_tick() -> int:
        """New successful payments in the ledger -> confirm any claim waiting for them (receipt, ticket closed)."""
        current = runtime.store.payments_by_utr
        new = [p for u, p in list(current.items()) if u not in seen_utrs]
        done = 0
        for p in new:
            seen_utrs.add(p["utr"])
            if p.get("status") != "success":
                continue
            try:
                res = runtime.ledger_caught_up(p)
            except Exception:  # noqa: BLE001
                logger.exception("ledger watcher failed for UTR ending %s", str(p.get("utr"))[-4:])
                continue
            if res and res.replies:
                _send_replies(res.conversation_id.split(":", 1)[-1], res.replies, res.conversation_id,
                              kind="ledger", turn_id=f"ledger:{p['utr']}")
                done += 1
        return done

    app.state.ledger_tick = ledger_tick

    @app.on_event("startup")
    async def _start_ledger_watcher():
        import asyncio

        async def loop():
            while True:
                try:
                    await asyncio.to_thread(ledger_tick)
                except Exception:  # noqa: BLE001
                    logger.exception("ledger watcher tick failed")
                await asyncio.sleep(15)
        app.state.ledger_watcher = asyncio.create_task(loop())

    # ---- console: send a payment reminder; post a claimed payment to the ledger (demo for the bank feed)
    def _reminder_allowed(mobile: str) -> bool:
        """Never message synthetic numbers on the live channel: only numbers that have talked to us (or are
        listed in SA_DEMO_REMINDER_NUMBERS)."""
        listed = {n.strip()[-10:] for n in (runtime.settings.demo_reminder_numbers or "").split(",") if n.strip()}
        if listed:                                 # set: only these numbers can get a reminder (demo lock)
            return mobile[-10:] in listed
        return runtime.conversations.get(f"wa:{mobile[-10:]}") is not None

    @app.get("/admin/api/conversations/{conversation_id}/loans")
    def conversation_loans(conversation_id: str):
        """The loans this conversation's number is an active contact on (the console's reminder picker)."""
        mobile = conversation_id.split(":", 1)[-1][-10:]
        out = []
        for acc_id, links in runtime.store.links_by_account.items():
            for l in links:
                c = runtime.store.contacts.get(l["contact_id"])
                if c and c.get("is_active") and c.get("mobile") == mobile:
                    a = runtime.store.accounts.get(acc_id) or {}
                    out.append({"account_id": acc_id, "overdue_amount": float(a.get("overdue_amount") or 0),
                                "emi_amount": float(a.get("emi_amount") or 0), "product": a.get("product")})
                    break
        return sorted(out, key=lambda x: x["account_id"])

    @app.post("/admin/api/reminders/{account_id}")
    def send_reminder(account_id: str, conversation_id: str | None = None):
        acc_contacts = [runtime.store.contacts.get(l["contact_id"]) for l in runtime.store.links_by_account.get(account_id, [])]
        mobiles = [c["mobile"] for c in acc_contacts if c and c.get("is_active")]
        if account_id not in runtime.store.accounts:
            raise HTTPException(404, detail="no such loan")
        allowed = [m for m in mobiles if _reminder_allowed(m)]
        if not conversation_id and len(allowed) > 1:
            raise HTTPException(409, detail="this loan has more than one number that has chatted with us; send the "
                                            "reminder from that borrower's conversation (refresh the console page)")
        if conversation_id:
            # Sent from a conversation in the console: to that number only (a loan can have several numbers).
            want = conversation_id.split(":", 1)[-1][-10:]
            if want not in [m[-10:] for m in mobiles]:
                raise HTTPException(409, detail="that conversation's number isn't an active contact on this loan")
            if not _reminder_allowed(want):
                raise HTTPException(409, detail="reminders are limited to SA_DEMO_REMINDER_NUMBERS; this number isn't listed")
            allowed = [want]
        if not allowed:
            raise HTTPException(409, detail="this loan's number hasn't messaged us (demo safety: no messages to "
                                            "synthetic numbers); add it to SA_DEMO_REMINDER_NUMBERS to allow it")
        try:
            r = runtime.payment_reminder(account_id, allowed[0])
        except LookupError as exc:
            raise HTTPException(409, detail=str(exc))
        runtime.channel.send_buttons(r["to"], r["text"], r["buttons"])
        logger.info("REMINDER sent for %s to %s", account_id, r["conversation_id"])
        return {"sent": True, "conversation_id": r["conversation_id"], "amount": r["amount"], "text": r["text"]}

    @app.post("/admin/api/claims/post-to-ledger")
    def post_claim_to_ledger(body: dict):
        """Demo: the bank feed catches up. Writes the claimed UTR and amount to the ledger; the watcher then
        confirms the claim (within the 2 minutes, or after an escalation) and sends the receipt."""
        from datetime import date
        account_id, utr = body.get("account_id"), body.get("utr")
        claim = runtime.claims.get(account_id, utr) if (account_id and utr) else None
        if not claim:
            raise HTTPException(404, detail="no such claim")
        if claim.get("result") not in ("PENDING", "NOT_FOUND") or utr in runtime.store.payments_by_utr:
            raise HTTPException(409, detail=f"nothing to post: this payment is already in the ledger "
                                            f"(claim {claim.get('result')}); only claims still being checked "
                                            f"(PENDING / NOT_FOUND) can be posted")
        today = date.today().isoformat()
        txn = {"txn_id": f"FEED{utr[-8:]}", "utr": utr, "account_id": account_id, "amount": float(claim["amount"] or 0),
               "channel": "UPI", "status": "success", "txn_date": claim.get("txn_date") or today,
               "settled_date": today, "payee_vpa": "collect@cnbank"}
        runtime.store.add_payment(txn)
        confirmed = ledger_tick()
        return {"posted": True, "utr_last4": utr[-4:], "amount": txn["amount"], "confirmed_claims": confirmed}

    @app.on_event("startup")
    async def _start_waiver_expiry():
        """Every minute: approved same-day waivers not paid by 23:59 IST are withdrawn (and the borrower told)."""
        import asyncio

        async def loop():
            while True:
                try:
                    if getattr(runtime, "paylinks", None) and hasattr(runtime.paylinks, "expire_due"):
                        await asyncio.to_thread(runtime.paylinks.expire_due)
                except Exception:  # noqa: BLE001
                    logger.exception("waiver expiry check failed")
                await asyncio.sleep(60)
        app.state.waiver_expiry = asyncio.create_task(loop())

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
        if body.followups:
            _schedule_followups(body.sender, result)
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

    # ---- mock payments console (single page; lists all pending links + pay any account in real time)
    @app.get("/pay", response_class=HTMLResponse)
    def pay_console():
        return HTMLResponse(_PAY_CONSOLE_HTML)

    @app.get("/pay/pending")
    def pay_pending():
        return runtime.paylinks.list_pending() if getattr(runtime, "paylinks", None) else []

    # ---- mock payment checkout (the clickable per-link page, opened from a WhatsApp link)
    @app.get("/pay/{link_id}", response_class=HTMLResponse)
    def pay_page(link_id: str):
        link = runtime.paylinks.get(link_id) if getattr(runtime, "paylinks", None) else None
        if not link:
            return HTMLResponse(_pay_html("Link not found", "<p>This payment link is invalid or has expired.</p>"),
                                status_code=404)
        if link["status"] != "pending":
            paid = link["status"] in ("paid",)
            body = (f"<p class='ok'>✓ Paid</p><p>₹{link['amount']:,.2f} · UTR {link.get('utr') or '—'}</p>"
                    if paid else "<p>This link is no longer active (a newer link was issued).</p>")
            return HTMLResponse(_pay_html("Payment", body))
        body = (f"<p class='amt'>₹{link['amount']:,.2f}</p>"
                f"<p class='acct'>Loan {_mask_acct(link['account_id'])}</p>"
                f"<form method='post' action='/pay/{link_id}/confirm'>"
                f"<button type='submit'>Pay ₹{link['amount']:,.2f}</button></form>"
                f"<p class='note'>This is a mock payment for testing — no real money moves.</p>")
        return HTMLResponse(_pay_html("Pay your loan", body))

    @app.post("/pay/{link_id}/confirm", response_class=HTMLResponse)
    def pay_confirm(link_id: str):
        if not getattr(runtime, "paylinks", None):
            raise HTTPException(503, detail="pay-links not available")
        res = runtime.paylinks.pay(link_id)
        if res.get("status") == "not_found":
            return HTMLResponse(_pay_html("Link not found", "<p>This payment link is invalid.</p>"), status_code=404)
        if res.get("status") in ("paid", "already_paid"):
            extra = "received ✓" if res["status"] == "paid" else "was already received ✓"
            body = (f"<p class='ok'>Payment {extra}</p>"
                    f"<p>₹{res['amount']:,.2f} · UTR {res.get('utr') or '—'}</p>"
                    + (f"<p class='acct'>Overdue now ₹{res['overdue_after']:,.2f}</p>" if 'overdue_after' in res else "")
                    + "<p class='note'>A confirmation and receipt have been sent on WhatsApp.</p>")
            return HTMLResponse(_pay_html("Payment successful", body))
        return HTMLResponse(_pay_html("Payment", "<p>This link is no longer active.</p>"))

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
