"""Inbound borrower-initiated flow (owner: T1). Phase 1.

A borrower messages the number first:
    "hi"  -> greeting + menu buttons (Account status / Statement / Payment info)
    pick  -> ask for the loan account number; if the sender is an active registered contact on
             that account, email an OTP (same neutral reply either way; nothing shown before OTP)
    OTP   -> verify, bind the session scope, then the agent serves the chosen intent
    (already verified: another menu pick is served directly, no second OTP)

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
from servicing_agent.platform.email import synthesize_email

logger = logging.getLogger(__name__)

_GREETING = re.compile(r"^\s*(hi|hello|hey|hii+|yo|namaste|namaskara|hola|good (morning|afternoon|evening)|menu|start|help)\b",
                       re.IGNORECASE)
_OTP = re.compile(r"^\s*\d{4,8}\s*$")
_SWITCH = re.compile(r"\b(switch|change)\s+(my\s+|the\s+)?(loan|account)\b|^\s*switch\s*$", re.IGNORECASE)

MENU = [
    {"id": "ACCOUNT_STATUS", "title": "Account status"},
    {"id": "GEN_SOA", "title": "Statement (SOA)"},
    {"id": "PAY_INFO", "title": "Payment info"},
]
SERVE_TEXT = {
    "ACCOUNT_STATUS": "show my account status",
    "GEN_SOA": "send my statement",
    "PAY_INFO": "show my payment history",
}
GREETING_MSG = ("Hello! Welcome to CreditNirvana servicing. How can I help you today? "
                "Please choose an option below.")


class InboundFlow:
    def __init__(self, store: DataStore, channel: Channel, email, conversations, settings: Settings, otp=None):
        self.store = store
        self.channel = channel
        self.email = email
        self.conversations = conversations
        self.settings = settings
        self.otp = otp  # RedisOtpService

    def _cid(self, sender: str) -> str:
        return f"wa:{normalize_mobile(sender)}"

    # Verification constraints (docs/DECISIONS.md): account number + the sender being an ACTIVE
    # registered contact on that account + OTP. A mobile number is never a lookup key on its own
    # (numbers can be shared between customers), nothing about any account is shown before the
    # OTP, and the reply is the same whether or not the details match.
    MAX_FAILURES_24H = 5

    def _active_contact(self, account_id: str, sender: str) -> dict | None:
        mobile = normalize_mobile(sender)
        for link in self.store.links_by_account.get(account_id, []):
            c = self.store.contacts.get(link["contact_id"])
            if c and c.get("is_active") and c.get("mobile") == mobile:
                return c
        return None

    def _failures(self, sess: dict) -> list[str]:
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        sess["inbound_failures"] = [t for t in sess.get("inbound_failures", []) if t >= cutoff]
        return sess["inbound_failures"]

    def _record_failure(self, sess: dict) -> int:
        fails = self._failures(sess)
        fails.append(datetime.utcnow().isoformat())
        return len(fails)

    def _ask_account(self, sender: str, sess: dict, selected: str | None) -> dict:
        sess["inbound"] = {"stage": "awaiting_account", "intent": selected}
        self.channel.send_text(sender, "To verify you, please share your loan account number (it starts with CN).")
        return {"handled": True}

    def _serve_verified(self, sess: dict, selected: str) -> dict:
        """Already verified for this session's account: no second OTP for another menu option."""
        sess["inbound"] = {"stage": "verified"}
        serve = SERVE_TEXT.get(selected)
        return {"handled": True, "serve": [serve] if serve else []}

    def _accounts_for_sender(self, sender: str) -> list[str]:
        """Accounts on which this number is an ACTIVE registered contact (via account_contacts)."""
        mobile = normalize_mobile(sender)
        out = []
        for account_id, links in self.store.links_by_account.items():
            for link in links:
                c = self.store.contacts.get(link["contact_id"])
                if c and c.get("is_active") and c.get("mobile") == mobile:
                    out.append(account_id)
                    break
        return out

    def _mask(self, account_id: str) -> str:
        # Only first-4 + last-4 of the account number.
        return account_id if len(account_id) <= 8 else f"{account_id[:4]}••••{account_id[-4:]}"

    def _offer_accounts(self, sender: str, sess: dict, selected: str) -> dict:
        """After a menu pick: 0 known loans -> ask for the account number; 1 -> straight to OTP;
        many -> a loan chooser (masked). Every path still re-checks active-contact before the OTP."""
        accounts = self._accounts_for_sender(sender)
        if not accounts:
            return self._ask_account(sender, sess, selected)
        if len(accounts) == 1:
            return self._start_otp(sender, sess, selected=selected, account_id=accounts[0])
        rows = [{"id": a, "title": self._mask(a)} for a in accounts]
        self.channel.send_list(sender, "Which loan is this about?", "Choose loan", rows)
        sess["inbound"] = {"stage": "awaiting_account_choice", "intent": selected, "candidates": accounts}
        logger.info("INBOUND loan chooser sent to %s (%d loans)", sender, len(accounts))
        return {"handled": True}

    SESSION_ENDED_MSG = "For your security, your earlier session has ended, so let's verify you again."

    def remember_for_switch(self, msg: InboundMessage) -> None:
        """The receipt belongs to the borrower's other loan: keep it so it's checked after SWITCH LOAN."""
        sess = self.conversations.load(self._cid(msg.sender))["session"]
        sess["inbound_replay"] = msg.model_dump(mode="json")

    def verify_for_message(self, msg: InboundMessage, expired: bool = False) -> dict:
        """A question that needs verification arrived with no verified session (never verified, or the
        session expired). Use the same path as the menu: loan chooser / OTP, then answer the
        original message (text, screenshot or voice) once verified."""
        sess = self.conversations.load(self._cid(msg.sender))["session"]
        sess["inbound_replay"] = msg.model_dump(mode="json")
        if expired:
            self.channel.send_text(msg.sender, self.SESSION_ENDED_MSG)
        logger.info("INBOUND verification for a question from %s (expired=%s)", msg.sender, expired)
        return self._offer_accounts(msg.sender, sess, selected=None)

    def handle(self, msg: InboundMessage) -> dict | None:
        """Returns a result dict if this flow handled the message (it sent replies itself),
        optionally with ``serve`` texts for the agent to run; None to let the agent handle it."""
        sess = self.conversations.load(self._cid(msg.sender))["session"]
        inb = sess.get("inbound") or {}
        stage = inb.get("stage")
        text = (msg.text or "").strip()

        # 1) greeting ALWAYS (re)opens the menu — even for an already-verified session.
        if _GREETING.search(text) and msg.type != "button" and stage != "awaiting_otp":
            self.channel.send_buttons(msg.sender, GREETING_MSG, MENU)
            sess["inbound"] = {"stage": "menu"}
            sess.pop("inbound_replay", None)          # starting over: forget an earlier question
            logger.info("INBOUND menu sent to %s", msg.sender)
            return {"handled": True}

        # 1b) switch to another of the borrower's loans (e.g. a payment was made to the other loan):
        #     same chooser + code; whatever the agent kept to replay (the receipt) is checked after.
        if _SWITCH.search(text) and stage != "awaiting_otp":
            logger.info("INBOUND switch loan for %s", msg.sender)
            return self._offer_accounts(msg.sender, sess, selected=None)

        # 2) verified and not mid-menu-flow -> let the agent handle it directly.
        if sess.get("scope") and stage in (None, "verified"):
            return None

        # 3) menu selection (button payload)
        if msg.type == "button" and text in SERVE_TEXT and stage in (None, "menu"):
            if sess.get("scope"):
                return self._serve_verified(sess, text)
            return self._offer_accounts(msg.sender, sess, text)

        # 3b) choosing which loan (sender has several)
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

        # 4) awaiting the loan account number. Only the FORMAT is checked out loud; whether the
        #    account exists or this number is registered on it is never revealed.
        if stage == "awaiting_account":
            acc = extract_account_number(text)
            if not acc or not is_valid_account_number(acc):
                self.channel.send_text(msg.sender, "That doesn't look like a valid loan account number "
                                                   "(it starts with CN and has 10 digits). Please try again.")
                return {"handled": True}
            return self._start_otp(msg.sender, sess, selected=inb.get("intent"), account_id=acc)

        # 5) awaiting OTP
        if stage == "awaiting_otp" and _OTP.match(text):
            return self._verify(msg.sender, sess, inb, text)

        return None

    NEUTRAL_OTP_MSG = ("If these details match our records, we've emailed a verification code to the "
                       "email registered on this loan. Please reply here with the code.")

    def _start_otp(self, sender: str, sess: dict, selected: str, account_id: str) -> dict:
        if len(self._failures(sess)) >= self.MAX_FAILURES_24H:
            sess["inbound"] = {}
            self.channel.send_text(sender, "For your security, verification is paused for a while. "
                                           "Please try again later or call our helpline.")
            logger.warning("INBOUND verification blocked for %s: repeated failures", sender)
            return {"handled": True}

        contact = self._active_contact(account_id, sender) if account_id in self.store.accounts else None
        if contact is None:
            # Unknown account, or this number isn't an active contact on it: same reply, no OTP.
            n = self._record_failure(sess)
            sess["inbound"] = {"stage": "awaiting_otp", "intent": selected, "account_id": None, "attempts": 0}
            self.channel.send_text(sender, self.NEUTRAL_OTP_MSG)
            logger.info("INBOUND no match for %s (failure %d in 24h)", sender, n)
            return {"handled": True}

        customer_id = self.store.accounts[account_id]["customer_id"]
        name = self.store.customers.get(customer_id, {}).get("name", "")
        # Dev override routes the OTP to a real inbox (synthetic customer emails don't exist).
        email = self.settings.otp_email_override or synthesize_email(name, customer_id, self.settings.email_domain)
        code = self.otp.issue(self._cid(sender))   # real random OTP in Redis (TTL)
        try:
            self.email.send_otp(email, code, name)
        except Exception:  # noqa: BLE001
            logger.exception("email OTP send failed")
        sess["inbound"] = {"stage": "awaiting_otp", "intent": selected, "account_id": account_id,
                           "customer_id": customer_id, "contact_id": contact["contact_id"], "email": email}
        self.channel.send_text(sender, self.NEUTRAL_OTP_MSG)
        logger.info("INBOUND otp emailed for %s", sender)
        return {"handled": True}

    def _verify(self, sender: str, sess: dict, inb: dict, code: str) -> dict:
        if not inb.get("account_id"):
            # No OTP was issued (details didn't match): behave exactly like a wrong code.
            inb["attempts"] = inb.get("attempts", 0) + 1
            left = max(self.settings.max_otp_attempts - inb["attempts"], 0)
            if left == 0:
                sess["inbound"] = {}
                self.channel.send_text(sender, "Too many incorrect attempts. Please try again later or call our helpline.")
            else:
                self.channel.send_text(sender, f"That code didn't match. Please try again ({left} left).")
            return {"handled": True}
        status, left = self.otp.verify(self._cid(sender), code)
        if status == "ok":
            self._bind_scope(sender, sess, inb)
            sess["inbound"] = {"stage": "verified"}
            self.channel.send_text(sender, "Verified ✅")
            serve = SERVE_TEXT.get(inb.get("intent"))
            replay = sess.pop("inbound_replay", None)
            logger.info("INBOUND verified %s -> serve %r replay=%s", sender, serve, bool(replay))
            return {"handled": True, "serve": [serve] if serve else [], "replay": replay}
        if status in ("expired", "locked"):
            sess.pop("inbound_replay", None)
        if status == "expired":
            sess["inbound"] = {}
            self.channel.send_text(sender, "That code has expired. Please say 'hi' to start again.")
        elif status == "locked":
            sess["inbound"] = {}
            self.channel.send_text(sender, "Too many incorrect attempts. Please try again later or call our helpline.")
        else:  # wrong
            self.channel.send_text(sender, f"That code didn't match. Please try again ({left} left).")
        return {"handled": True}

    def _bind_scope(self, sender: str, sess: dict, inb: dict) -> None:
        account_id = inb["account_id"]
        customer_id = inb["customer_id"]
        contact_id = inb["contact_id"]          # checked before the OTP was issued
        now = datetime.utcnow()
        scope = AuthScope(account_id=account_id, customer_id=customer_id, contact_id=contact_id,
                          method="account+mobile+otp", verified_at=now, expires_at=now + timedelta(minutes=120))
        sess["scope"] = scope.model_dump(mode="json")
