"""Inbound borrower-initiated flow (owner: T1). Phase 1.

A borrower messages the number first:
    "hi"  -> greeting + menu buttons (Account status / Statement / Payment info)
    pick  -> identify account from the sender's number, email an OTP
    OTP   -> verify, bind the session scope, then the agent serves the chosen intent

This is distinct from the outbound campaign journey (platform/campaigns.py). State lives
in the conversation session (`session['inbound']`). OTP is the dev stub `123456` and the
email is mock-logged unless SMTP is configured.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from servicing_agent.config import Settings
from servicing_agent.contracts.state import AuthScope, InboundMessage
from servicing_agent.data.identifiers import extract_account_number, is_valid_account_number, normalize_mobile
from servicing_agent.data.store import DataStore
from servicing_agent.platform.channel import Channel
from servicing_agent.platform.email import mask_email, synthesize_email

logger = logging.getLogger(__name__)

_GREETING = re.compile(r"^\s*(hi|hello|hey|hii+|yo|namaste|namaskara|hola|good (morning|afternoon|evening)|menu|start|help)\b",
                       re.IGNORECASE)
_OTP = re.compile(r"^\s*\d{4,8}\s*$")

MENU = [
    {"id": "ACCOUNT_STATUS", "title": "Account status"},
    {"id": "GEN_SOA", "title": "Statement (SOA)"},
    {"id": "PAY_INFO", "title": "Payment info"},
]
SERVE_TEXT = {
    "ACCOUNT_STATUS": "what is my outstanding amount",
    "GEN_SOA": "send my statement",
    "PAY_INFO": "show my payment history",
}
GREETING_MSG = ("Hello! Welcome to CreditNirvana servicing. How can I help you today? "
                "Please choose an option below.")


class InboundFlow:
    def __init__(self, store: DataStore, channel: Channel, email, conversations, settings: Settings):
        self.store = store
        self.channel = channel
        self.email = email
        self.conversations = conversations
        self.settings = settings

    def _cid(self, sender: str) -> str:
        return f"wa:{normalize_mobile(sender)}"

    def accounts_for_sender(self, sender: str) -> list[str]:
        """Accounts whose owner has an active contact with this number (borrower's own loans)."""
        mobile = normalize_mobile(sender)
        customer_ids = {c["customer_id"] for c in self.store.contacts.values()
                        if c.get("mobile") == mobile and c.get("is_active")}
        return [a["account_id"] for a in self.store.accounts.values() if a["customer_id"] in customer_ids]

    def handle(self, msg: InboundMessage) -> dict | None:
        """Returns a result dict if this flow handled the message (it sent replies itself),
        optionally with ``serve`` texts for the agent to run; None to let the agent handle it."""
        sess = self.conversations.load(self._cid(msg.sender))["session"]
        if sess.get("scope"):
            return None  # already verified -> agent handles
        inb = sess.get("inbound") or {}
        stage = inb.get("stage")
        text = (msg.text or "").strip()

        # 1) menu selection (button payload)
        if msg.type == "button" and text in SERVE_TEXT and stage in (None, "menu"):
            return self._start_otp(msg.sender, sess, selected=text)

        # 2) greeting -> menu
        if _GREETING.search(text) and stage != "awaiting_otp":
            self.channel.send_buttons(msg.sender, GREETING_MSG, MENU)
            sess["inbound"] = {"stage": "menu"}
            logger.info("INBOUND menu sent to %s", msg.sender)
            return {"handled": True}

        # 3a) choosing which loan (customer has several)
        if stage == "awaiting_account_choice":
            candidates = inb.get("candidates", [])
            chosen = text if text in candidates else None
            if chosen is None:
                acc = extract_account_number(text)
                chosen = acc if acc in candidates else None
            if chosen:
                return self._start_otp(msg.sender, sess, selected=inb.get("intent"), account_id=chosen)
            self.channel.send_text(msg.sender, "Please pick one of your loans from the list above.")
            return {"handled": True}

        # 3) awaiting a loan account number (fallback when we couldn't auto-identify)
        if stage == "awaiting_account":
            acc = extract_account_number(text)
            if acc and is_valid_account_number(acc) and acc in self.store.accounts:
                return self._start_otp(msg.sender, sess, selected=inb.get("intent"), account_id=acc)
            self.channel.send_text(msg.sender, "That doesn't look like a valid loan account number (starts with CN). Please try again.")
            return {"handled": True}

        # 4) awaiting OTP
        if stage == "awaiting_otp" and _OTP.match(text):
            return self._verify(msg.sender, sess, inb, text)

        return None

    def _start_otp(self, sender: str, sess: dict, selected: str, account_id: str | None = None) -> dict:
        if account_id is None:
            accounts = self.accounts_for_sender(sender)
            if not accounts:
                sess["inbound"] = {"stage": "awaiting_account", "intent": selected}
                self.channel.send_text(sender, "To verify you, please share your loan account number (it starts with CN).")
                return {"handled": True}
            if len(accounts) > 1:
                rows = [{"id": a, "title": self._mask(a), "description": self._acc_desc(a)} for a in accounts]
                self.channel.send_list(sender, "You have more than one loan with us. Which one is this about?",
                                       "Choose loan", rows)
                sess["inbound"] = {"stage": "awaiting_account_choice", "intent": selected, "candidates": accounts}
                logger.info("INBOUND account chooser sent to %s (%d loans)", sender, len(accounts))
                return {"handled": True}
            account_id = accounts[0]
        customer_id = self.store.accounts[account_id]["customer_id"]
        name = self.store.customers.get(customer_id, {}).get("name", "")
        # Dev override routes the OTP to a real inbox (synthetic customer emails don't exist).
        email = self.settings.otp_email_override or synthesize_email(name, customer_id, self.settings.email_domain)
        code = self.settings.otp_stub_code
        try:
            self.email.send_otp(email, code, name)
        except Exception:  # noqa: BLE001
            logger.exception("email OTP send failed")
        sess["inbound"] = {"stage": "awaiting_otp", "intent": selected, "account_id": account_id,
                           "customer_id": customer_id, "email": email, "attempts": 0}
        self.channel.send_text(sender, f"To continue, we've emailed a verification code to {mask_email(email)}. "
                                       "Please reply here with the code.")
        logger.info("INBOUND otp emailed for %s account=%s", sender, account_id)
        return {"handled": True}

    def _mask(self, account_id: str) -> str:
        return f"Loan ••{account_id[-4:]}"

    def _acc_desc(self, account_id: str) -> str:
        a = self.store.accounts.get(account_id, {})
        od = a.get("overdue_amount", 0) or 0
        return f"{a.get('product', 'loan')} · overdue ₹{od:,.0f}"

    def _verify(self, sender: str, sess: dict, inb: dict, code: str) -> dict:
        if code.strip() == self.settings.otp_stub_code:
            self._bind_scope(sender, sess, inb)
            sess["inbound"] = {"stage": "verified"}
            self.channel.send_text(sender, "Verified ✅")
            serve = SERVE_TEXT.get(inb.get("intent"))
            logger.info("INBOUND verified %s -> serve %r", sender, serve)
            return {"handled": True, "serve": [serve] if serve else []}
        attempts = int(inb.get("attempts", 0)) + 1
        if attempts >= self.settings.max_otp_attempts:
            sess["inbound"] = {}
            self.channel.send_text(sender, "Too many incorrect attempts. Please try again later or call our helpline.")
        else:
            inb["attempts"] = attempts
            sess["inbound"] = inb
            self.channel.send_text(sender, f"That code didn't match. Please try again ({self.settings.max_otp_attempts - attempts} left).")
        return {"handled": True}

    def _bind_scope(self, sender: str, sess: dict, inb: dict) -> None:
        account_id = inb["account_id"]
        customer_id = inb["customer_id"]
        mobile = normalize_mobile(sender)
        contact_id = next((c["contact_id"] for c in self.store.contacts.values()
                           if c.get("mobile") == mobile and c.get("customer_id") == customer_id), "")
        now = datetime.utcnow()
        scope = AuthScope(account_id=account_id, customer_id=customer_id, contact_id=contact_id,
                          method="account+mobile+otp", verified_at=now, expires_at=now + timedelta(minutes=120))
        sess["scope"] = scope.model_dump(mode="json")
