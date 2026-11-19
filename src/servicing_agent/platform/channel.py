"""WhatsApp channel adapter: the bot's wire to the borrower (owner: T1).

Two send paths, because WhatsApp enforces a 24-hour customer-service window:

* ``send_text``     — free-form, allowed only inside an open session (the borrower
                      messaged us within the last 24h). Use for normal replies.
* ``send_template`` — a Meta pre-approved template, allowed any time. Use to open or
                      re-open a session and for every compliance-gated outcome
                      (this is what moves a case to ``COMMUNICATED``).

The real adapter talks to the Meta Graph API; ``MockChannel`` records sends in memory
so ``/v1/simulate`` and the eval harness run with no network and no credentials.
Nothing here holds a secret: the token and phone-number id come from ``Settings``.
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from servicing_agent.config import Settings, get_settings

if TYPE_CHECKING:
    import httpx
from servicing_agent.contracts.state import InboundMessage

logger = logging.getLogger(__name__)

# Meta message "type" -> our InboundMessage.type. A voice note arrives as "audio".
_INBOUND_TYPE = {
    "text": "text",
    "button": "button",
    "interactive": "button",
    "image": "image",
    "audio": "voice",
}


def _our_type(msg: dict[str, Any], wa_type: str) -> str:
    """Map Meta's type to ours. A screenshot or PDF sent as a *file* arrives as "document";
    treat it as an image so it is downloaded and read like a photo."""
    if wa_type == "document":
        mime = ((msg.get("document") or {}).get("mime_type") or "").lower()
        if mime.startswith("image/") or mime == "application/pdf":
            return "image"
    return _INBOUND_TYPE.get(wa_type, "text")


class Channel(Protocol):
    """The outbound contract the turn handler codes against."""

    def send_text(self, to: str, text: str) -> dict[str, Any]:
        """Free-form reply. Valid only inside the 24h session window."""

    def send_template(
        self, to: str, template_id: str, language: str = "en_US",
        params: list[str] | None = None,
    ) -> dict[str, Any]:
        """Pre-approved template send. ``params`` fill the body placeholders, in order."""

    def send_buttons(self, to: str, body: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
        """Interactive reply buttons (in-session). ``buttons`` = [{id, title}], max 3."""

    def send_list(self, to: str, body: str, button_text: str, rows: list[dict[str, str]]) -> dict[str, Any]:
        """Interactive list (in-session). ``rows`` = [{id, title, description?}], up to 10."""

    def send_document(self, to: str, file_path: str, filename: str | None = None,
                      caption: str | None = None) -> dict[str, Any]:
        """Send a PDF (or other) file as a WhatsApp document attachment (in-session)."""


def _wa_number(to: str) -> str:
    """WhatsApp needs the full international number. Our stored mobiles are 10-digit Indian
    numbers; prepend the 91 country code. Leave already-prefixed numbers untouched."""
    digits = "".join(ch for ch in (to or "") if ch.isdigit())
    if len(digits) == 10:
        return "91" + digits
    return digits


def _template_payload(to: str, template_id: str, language: str, params: list[str] | None) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    if params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in params],
        })
    return {
        "messaging_product": "whatsapp",
        "to": _wa_number(to),
        "type": "template",
        "template": {
            "name": template_id,
            "language": {"code": language},
            "components": components,
        },
    }


def _list_payload(to: str, body: str, button_text: str, rows: list[dict[str, str]],
                  section_title: str = "Options") -> dict[str, Any]:
    # WhatsApp interactive list: up to 10 rows; title <=24, description <=72.
    return {
        "messaging_product": "whatsapp",
        "to": _wa_number(to),
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {"button": button_text[:20], "sections": [{
                "title": section_title[:24],
                "rows": [{"id": r["id"], "title": r["title"][:24],
                          **({"description": r["description"][:72]} if r.get("description") else {})}
                         for r in rows[:10]],
            }]},
        },
    }


def _text_payload(to: str, text: str) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "to": _wa_number(to),
        "type": "text",
        "text": {"body": text, "preview_url": False},
    }


def _buttons_payload(to: str, body: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
    # WhatsApp interactive reply buttons: max 3, title <= 20 chars.
    return {
        "messaging_product": "whatsapp",
        "to": _wa_number(to),
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                for b in buttons[:3]
            ]},
        },
    }


class WhatsAppChannel:
    """Live adapter for the WhatsApp Cloud API (Meta Graph).

    POSTs to ``/{version}/{phone_number_id}/messages``. The bearer token is read from
    settings and sent only in the Authorization header; it is never logged.
    """

    def __init__(self, settings: Settings | None = None, client: "httpx.Client | None" = None):
        s = settings or get_settings()
        if not s.whatsapp_token or not s.wa_phone_number_id:
            raise ValueError(
                "WhatsApp is not configured: set SA_WHATSAPP_TOKEN and SA_WA_PHONE_NUMBER_ID "
                "(use MockChannel for local/sim runs)."
            )
        if client is None:
            import httpx  # imported lazily so MockChannel/parsing need no HTTP dependency
            client = httpx.Client(timeout=10.0)
        self._token = s.whatsapp_token
        self._base = f"{s.graph_base_url}/{s.wa_api_version}"
        self._url = f"{self._base}/{s.wa_phone_number_id}/messages"
        self._client = client

    def download_media(self, media_id: str) -> tuple[bytes, str | None]:
        """Pull an inbound media file from Meta: GET /{media_id} -> url -> GET url (bytes).
        Returns (content, mime_type). Media URLs are short-lived, so fetch just-in-time."""
        auth = {"Authorization": f"Bearer {self._token}"}
        meta = self._client.get(f"{self._base}/{media_id}", headers=auth)
        if meta.status_code >= 400:
            raise RuntimeError(f"media lookup {meta.status_code}: {meta.text}")
        info = meta.json()
        url, mime = info.get("url"), info.get("mime_type")
        if not url:
            raise RuntimeError(f"media {media_id} has no url: {info}")
        blob = self._client.get(url, headers=auth)  # the CDN url also needs the Bearer token
        if blob.status_code >= 400:
            raise RuntimeError(f"media download {blob.status_code}")
        return blob.content, mime

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(
            self._url,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code >= 400:
            # Surface Meta's error body (never the request — it carries the token).
            logger.warning("whatsapp send failed: %s %s", resp.status_code, resp.text)
            raise RuntimeError(f"WhatsApp API {resp.status_code}: {resp.text}")
        return resp.json()

    def send_text(self, to: str, text: str) -> dict[str, Any]:
        return self._post(_text_payload(to, text))

    def send_template(
        self, to: str, template_id: str, language: str = "en_US",
        params: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._post(_template_payload(to, template_id, language, params))

    def send_buttons(self, to: str, body: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
        return self._post(_buttons_payload(to, body, buttons))

    def send_list(self, to: str, body: str, button_text: str, rows: list[dict[str, str]]) -> dict[str, Any]:
        return self._post(_list_payload(to, body, button_text, rows))

    def upload_media(self, file_path: str, mime_type: str = "application/pdf") -> str:
        """Upload a file to Meta's media store and return its media id (reusable for 30 days)."""
        import os
        url = self._url.replace("/messages", "/media")
        with open(file_path, "rb") as fh:
            resp = self._client.post(
                url,
                headers={"Authorization": f"Bearer {self._token}"},
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (os.path.basename(file_path), fh, mime_type)},
            )
        if resp.status_code >= 400:
            logger.warning("whatsapp media upload failed: %s %s", resp.status_code, resp.text)
            raise RuntimeError(f"WhatsApp media upload {resp.status_code}: {resp.text}")
        return resp.json()["id"]

    def send_document(self, to: str, file_path: str, filename: str | None = None,
                      caption: str | None = None) -> dict[str, Any]:
        import os
        media_id = self.upload_media(file_path)
        doc: dict[str, Any] = {"id": media_id, "filename": filename or os.path.basename(file_path)}
        if caption:
            doc["caption"] = caption
        return self._post({"messaging_product": "whatsapp", "recipient_type": "individual",
                           "to": _wa_number(to), "type": "document", "document": doc})

    def close(self) -> None:
        self._client.close()


class MockChannel:
    """In-memory channel. Records every send; returns a synthetic message id.

    Use for ``/v1/simulate``, local runs and tests. ``sent`` is the full outbound log.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._ids = itertools.count(1)

    def _record(self, kind: str, to: str, payload: dict[str, Any]) -> dict[str, Any]:
        mid = f"wamid.MOCK{next(self._ids):06d}"
        self.sent.append({"kind": kind, "to": to, "message_id": mid, "payload": payload})
        return {"messages": [{"id": mid}], "contacts": [{"wa_id": to}]}

    def send_text(self, to: str, text: str) -> dict[str, Any]:
        return self._record("text", to, _text_payload(to, text))

    def send_template(
        self, to: str, template_id: str, language: str = "en_US",
        params: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._record("template", to, _template_payload(to, template_id, language, params))

    def send_buttons(self, to: str, body: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
        return self._record("buttons", to, _buttons_payload(to, body, buttons))

    def send_list(self, to: str, body: str, button_text: str, rows: list[dict[str, str]]) -> dict[str, Any]:
        return self._record("list", to, _list_payload(to, body, button_text, rows))

    def download_media(self, media_id: str) -> tuple[bytes, str | None]:
        return b"MOCK-MEDIA", "application/octet-stream"

    def upload_media(self, file_path: str, mime_type: str = "application/pdf") -> str:
        return f"media.MOCK{next(self._ids):06d}"

    def send_document(self, to: str, file_path: str, filename: str | None = None,
                      caption: str | None = None) -> dict[str, Any]:
        import os
        payload = {"type": "document", "document": {"filename": filename or os.path.basename(file_path),
                                                     "path": file_path, "caption": caption}}
        return self._record("document", to, payload)


def _media_ref(msg: dict[str, Any], wa_type: str) -> tuple[str | None, str | None]:
    """Meta's media handle for image/voice/etc: the id + mime. The file is fetched later."""
    obj = msg.get(wa_type) if wa_type in ("image", "audio", "video", "document", "sticker") else None
    if not isinstance(obj, dict):
        return None, None
    return obj.get("id"), obj.get("mime_type")


def _extract_text(msg: dict[str, Any], wa_type: str) -> str | None:
    """Pull the user-visible text/payload out of a raw WhatsApp message."""
    if wa_type == "text":
        return (msg.get("text") or {}).get("body")
    if wa_type == "button":                       # quick-reply template button
        return (msg.get("button") or {}).get("payload") or (msg.get("button") or {}).get("text")
    if wa_type == "interactive":                  # list / reply-button
        inter = msg.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("id") or reply.get("title")
    if wa_type in ("image", "document", "video"):  # caption typed with the media ("I have paid")
        caption = (msg.get(wa_type) or {}).get("caption")
        return caption.strip() if isinstance(caption, str) and caption.strip() else None
    return None


def parse_inbound_webhook(body: dict[str, Any]) -> list[InboundMessage]:
    """Flatten a Meta webhook payload into ``InboundMessage`` objects.

    Media (image/voice) arrives as a Meta media id, not bytes: this parser sets the
    message ``type`` and leaves ``media`` unset. The orchestrator downloads the media
    and runs the perception pipeline (T3) to produce ``MediaEvidence`` before the agent
    ever sees it. Status callbacks (delivered/read) carry no ``messages`` and are skipped.
    """
    out: list[InboundMessage] = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                wa_type = msg.get("type", "")
                received_at = datetime.now(timezone.utc)
                ts = msg.get("timestamp")
                if ts:
                    try:
                        received_at = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                    except (ValueError, OSError):
                        pass
                media_id, media_mime = _media_ref(msg, wa_type)
                out.append(InboundMessage(
                    message_id=msg.get("id", ""),
                    sender=msg.get("from", ""),
                    type=_our_type(msg, wa_type),
                    text=_extract_text(msg, wa_type),
                    media=None,
                    media_id=media_id,
                    media_mime=media_mime,
                    received_at=received_at,
                ))
    return out


def verify_webhook(mode: str | None, token: str | None, challenge: str | None,
                   settings: Settings | None = None) -> str | None:
    """Meta webhook verification handshake (GET). Returns the challenge to echo, or None.

    Wire this to ``GET /webhook`` with query params ``hub.mode``, ``hub.verify_token``,
    ``hub.challenge``.
    """
    s = settings or get_settings()
    if mode == "subscribe" and token and s.wa_verify_token and token == s.wa_verify_token:
        return challenge
    return None
