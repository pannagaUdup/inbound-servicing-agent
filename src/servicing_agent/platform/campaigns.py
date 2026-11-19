"""Outbound campaign service (owner: T1 / admin panel). Phase 1.

An admin selects accounts; we resolve each account's active registered contact from
our DB, send an outbound message, and track each account's journey:

    queued -> sent -> acknowledged -> otp_sent -> verified -> awaiting_payment -> closed
                   \\-> no active contact: skipped

The outbound message is a placeholder for now ("send any message"); a Meta-approved
template replaces it later. Acknowledgement is decided by the (mocked) ack classifier.
OTP uses the existing OTP tool. Payment handling is deferred — after verification the
journey parks at ``awaiting_payment`` with the conversation scope bound, so the agent
can take over when the payment flow is built.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from servicing_agent.agent.ack import AckClassifier, make_ack_classifier
from servicing_agent.config import Settings
from servicing_agent.contracts.state import AuthScope
from servicing_agent.data.identifiers import normalize_mobile
from servicing_agent.data.store import DataStore
from servicing_agent.platform.channel import Channel, MockChannel
from servicing_agent.platform.db import Campaign, CampaignTarget

DEFAULT_MESSAGE = ("Hello from CreditNirvana about your loan account. "
                   "Please reply to continue — we need to confirm a few details.")


def mobile_hash(raw: str) -> str:
    return hashlib.sha256(normalize_mobile(raw).encode()).hexdigest()[:32]


def _primary_active_contact(store: DataStore, account_id: str) -> dict | None:
    """The account's active contact to message: an active borrower first, else any active contact."""
    links = store.links_by_account.get(account_id, [])
    actives = [(l, store.contacts[l["contact_id"]]) for l in links
               if l["contact_id"] in store.contacts and store.contacts[l["contact_id"]]["is_active"]]
    if not actives:
        return None
    actives.sort(key=lambda lc: 0 if lc[0]["role"] == "borrower" else 1)
    link, ct = actives[0]
    return {"contact_id": ct["contact_id"], "customer_id": ct["customer_id"],
            "mobile": ct["mobile"], "role": link["role"]}


class CampaignService:
    def __init__(self, session_factory, store: DataStore, channel: Channel | None = None,
                 tools=None, settings: Settings | None = None, conversations=None,
                 ack: AckClassifier | None = None):
        self.Session = session_factory
        self.store = store
        self.channel = channel or MockChannel()
        self.tools = tools
        self.settings = settings
        self.conversations = conversations
        self.ack = ack or make_ack_classifier("mock")

    # ---------- account selection (admin picks from here) ----------
    def selectable_accounts(self, *, product: str | None = None, dpd_min: int | None = None,
                            q: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        ql = q.strip().upper() if q else None
        out = []
        for a in self.store.accounts.values():
            if product and a["product"] != product:
                continue
            if dpd_min is not None and a["dpd"] < dpd_min:
                continue
            if ql:
                cust = self.store.customers.get(a["customer_id"], {})
                hay = f"{a['account_id']} {cust.get('city','')}".upper()
                if ql not in hay:
                    continue
            contact = _primary_active_contact(self.store, a["account_id"])
            cust = self.store.customers.get(a["customer_id"], {})
            out.append({
                "account_id": a["account_id"], "product": a["product"], "dpd": a["dpd"],
                "overdue_amount": a["overdue_amount"], "emi_amount": a["emi_amount"],
                "city": cust.get("city"), "language": cust.get("preferred_language"),
                "contactable": contact is not None,
            })
            if len(out) >= limit:
                break
        out.sort(key=lambda r: r["dpd"], reverse=True)
        return out

    # ---------- create / launch ----------
    def create(self, name: str, account_ids: list[str], *, message_body: str | None = None,
               template_id: str | None = None, template_lang: str = "en_US",
               template_params: list[str] | None = None, created_by: str | None = None) -> dict:
        campaign_id = f"CMP{uuid.uuid4().hex[:8].upper()}"
        with self.Session.begin() as s:
            s.add(Campaign(campaign_id=campaign_id, name=name, created_by=created_by,
                           message_body=message_body or DEFAULT_MESSAGE, template_id=template_id,
                           template_lang=template_lang, template_params=template_params,
                           status="draft", total_targets=len(account_ids)))
            for acc in account_ids:
                contact = _primary_active_contact(self.store, acc)
                s.add(CampaignTarget(
                    campaign_id=campaign_id, account_id=acc,
                    contact_id=contact["contact_id"] if contact else None,
                    to_mobile_hash=mobile_hash(contact["mobile"]) if contact else None,
                    state="queued" if contact else "skipped",
                    last_error=None if contact else "no active contact on account",
                ))
        return self.get(campaign_id)

    def launch(self, campaign_id: str) -> dict:
        with self.Session.begin() as s:
            camp = s.get(Campaign, campaign_id)
            if not camp:
                raise KeyError(campaign_id)
            camp.status = "running"
            camp.started_at = datetime.utcnow()
            targets = s.execute(select(CampaignTarget).where(
                CampaignTarget.campaign_id == campaign_id, CampaignTarget.state == "queued")).scalars().all()
            body = camp.message_body or DEFAULT_MESSAGE
            for t in targets:
                contact = self.store.contacts.get(t.contact_id or "")
                if not contact:
                    t.state, t.last_error = "skipped", "contact vanished"
                    continue
                try:
                    if camp.template_id:
                        res = self.channel.send_template(contact["mobile"], camp.template_id,
                                                         language=camp.template_lang or "en_US",
                                                         params=camp.template_params)
                    else:
                        res = self.channel.send_text(contact["mobile"], body)
                    t.out_message_id = (res.get("messages") or [{}])[0].get("id")
                    t.conversation_id = f"wa:{normalize_mobile(contact['mobile'])}"
                    t.state, t.sent_at = "sent", datetime.utcnow()
                except Exception as exc:  # noqa: BLE001 - record and move on
                    t.state, t.last_error = "failed", str(exc)[:200]
        return self.get(campaign_id)

    # ---------- reads ----------
    def list(self) -> list[dict]:
        with self.Session() as s:
            camps = s.execute(select(Campaign).order_by(Campaign.created_at.desc())).scalars().all()
            counts = dict(s.execute(
                select(CampaignTarget.campaign_id, func.count()).group_by(CampaignTarget.campaign_id)).all())
            return [{"campaign_id": c.campaign_id, "name": c.name, "status": c.status,
                     "total_targets": c.total_targets, "targets": counts.get(c.campaign_id, 0),
                     "created_at": c.created_at.isoformat(timespec="seconds")} for c in camps]

    def get(self, campaign_id: str) -> dict:
        with self.Session() as s:
            c = s.get(Campaign, campaign_id)
            if not c:
                raise KeyError(campaign_id)
            targets = s.execute(select(CampaignTarget).where(CampaignTarget.campaign_id == campaign_id)
                                .order_by(CampaignTarget.id)).scalars().all()
            by_state: dict[str, int] = {}
            for t in targets:
                by_state[t.state] = by_state.get(t.state, 0) + 1
            return {
                "campaign_id": c.campaign_id, "name": c.name, "status": c.status,
                "message_body": c.message_body, "template_id": c.template_id,
                "created_by": c.created_by, "total_targets": c.total_targets,
                "created_at": c.created_at.isoformat(timespec="seconds"),
                "by_state": by_state,
                "targets": [{"account_id": t.account_id, "state": t.state, "contact_id": t.contact_id,
                             "out_message_id": t.out_message_id, "conversation_id": t.conversation_id,
                             "last_error": t.last_error} for t in targets],
            }

    # ---------- inbound journey ----------
    def target_by_sender(self, sender: str) -> CampaignTarget | None:
        h = mobile_hash(sender)
        with self.Session() as s:
            return s.execute(
                select(CampaignTarget)
                .where(CampaignTarget.to_mobile_hash == h,
                       CampaignTarget.state.notin_(["closed", "skipped", "failed"]))
                .order_by(CampaignTarget.id.desc())
            ).scalars().first()

    def handle_reply(self, sender: str, text: str | None) -> dict | None:
        """Advance the campaign journey for an inbound reply. Returns a summary, or None
        if the sender is not an open campaign target (caller then runs the normal agent)."""
        h = mobile_hash(sender)
        to = normalize_mobile(sender)
        sent: list[str] = []
        with self.Session.begin() as s:
            t = s.execute(
                select(CampaignTarget)
                .where(CampaignTarget.to_mobile_hash == h,
                       CampaignTarget.state.notin_(["closed", "skipped", "failed"]))
                .order_by(CampaignTarget.id.desc())
            ).scalars().first()
            if t is None:
                return None
            j = dict(t.journey or {})

            if t.state in ("sent", "delivered", "read", "acknowledged"):
                if self.ack.is_ack(text):
                    t.state, t.acknowledged_at = "acknowledged", datetime.utcnow()
                    req_id = self.tools.otp.send(to) if self.tools else "mock"
                    j["otp_request_id"] = req_id
                    j["otp_attempts"] = 0
                    t.state = "otp_sent"
                    msg = "Thanks! To confirm it's you, please reply with the OTP we just sent."
                    self.channel.send_text(to, msg); sent.append(msg)
                else:
                    msg = "No problem — reply here whenever you're ready to continue."
                    self.channel.send_text(to, msg); sent.append(msg)

            elif t.state == "otp_sent":
                attempts = int(j.get("otp_attempts", 0)) + 1
                j["otp_attempts"] = attempts
                code = (text or "").strip()
                ok = self.tools.otp.verify(j.get("otp_request_id", ""), code) if self.tools else (code == "123456")
                max_attempts = self.settings.max_otp_attempts if self.settings else 3
                if ok:
                    t.state, t.verified_at = "verified", datetime.utcnow()
                    self._bind_scope(t, to)
                    t.state = "awaiting_payment"
                    msg = "Verified. Have you made your payment for this loan? Please reply with the details."
                    self.channel.send_text(to, msg); sent.append(msg)
                elif attempts >= max_attempts:
                    t.state, t.last_error = "closed", "otp attempts exhausted"
                    t.closed_at = datetime.utcnow()
                    msg = "Too many incorrect attempts. Please contact our helpline for assistance."
                    self.channel.send_text(to, msg); sent.append(msg)
                else:
                    msg = f"That OTP didn't match. Please try again ({max_attempts - attempts} left)."
                    self.channel.send_text(to, msg); sent.append(msg)

            else:  # awaiting_payment / verified -> payment handling deferred to the agent later
                t.journey = j
                return {"account_id": t.account_id, "state": t.state, "sent": sent,
                        "handoff_to_agent": True}

            t.journey = j
            return {"account_id": t.account_id, "state": t.state, "sent": sent, "handoff_to_agent": False}

    def _bind_scope(self, t: CampaignTarget, mobile: str) -> None:
        """Bind a verified scope into the conversation session so the agent sees it as verified."""
        if not (self.conversations and t.conversation_id):
            return
        contact = self.store.contacts.get(t.contact_id or "")
        if not contact:
            return
        now = datetime.utcnow()
        ttl = timedelta(minutes=120)
        scope = AuthScope(account_id=t.account_id, customer_id=contact["customer_id"],
                          contact_id=contact["contact_id"], method="account+mobile+otp",
                          verified_at=now, expires_at=now + ttl)
        convo = self.conversations.load(t.conversation_id)
        convo["session"]["scope"] = scope.model_dump(mode="json")
