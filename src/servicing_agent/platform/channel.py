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


class Channel(Protocol):
    """The outbound contract the turn handler codes against."""

    def send_text(self, to: str, text: str) -> dict[str, Any]:
        """Free-form reply. Valid only inside the 24h session window."""

    def send_template(
        self, to: str, template_id: str, language: str = "en_US",
        params: list[str] | None = None,
    ) -> dict[str, Any]:
        """Pre-approved template send. ``params`` fill the body placeholders, in order."""


def _template_payload(to: str, template_id: str, language: str, params: list[str] | None) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    if params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in params],
        })
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_id,
            "language": {"code": language},
            "components": components,
        },
    }


def _text_payload(to: str, text: str) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text, "preview_url": False},
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
        self._url = f"{s.graph_base_url}/{s.wa_api_version}/{s.wa_phone_number_id}/messages"
        self._client = client

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
                out.append(InboundMessage(
                    message_id=msg.get("id", ""),
                    sender=msg.get("from", ""),
                    type=_INBOUND_TYPE.get(wa_type, "text"),
                    text=_extract_text(msg, wa_type),
                    media=None,
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
