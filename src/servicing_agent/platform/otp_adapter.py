"""Real OTP for the agent's own verification step (owner: T2).

The agent (agent/verification.py) talks to an ``OTPService`` (contracts/tools.py). In dev and
tests that's ``MockOTP`` (accepts the stub code, sends nothing). On the live WhatsApp channel it
must be real, otherwise anyone who knows the stub code could verify. This adapter reuses the
inbound flow's Redis OTP store and email sender, so both verification paths behave the same:

* ``send(mobile, account_id)`` -> random 6-digit code in Redis (TTL, attempt limit), emailed to
  the account's registered email. Returns a request id, or None if it couldn't issue one
  (Redis down) - verification then simply fails: fail closed.
* ``verify(request_id, code)`` -> True only for the right code within the TTL and attempts.
"""

from __future__ import annotations

import logging
import uuid

from servicing_agent.platform.contact_email import resolve_email

log = logging.getLogger(__name__)


class AgentOtpAdapter:
    def __init__(self, otp_store, email_sender, store, settings, emails=None):
        self.emails = emails              # ContactEmails: email on file per mobile
        self.otp = otp_store              # platform/otp.py RedisOtpService (issue / verify)
        self.email = email_sender
        self.store = store
        self.settings = settings

    def send(self, mobile: str, account_id: str | None = None) -> str | None:
        if not account_id or account_id not in self.store.accounts:
            return None
        customer_id = self.store.accounts[account_id]["customer_id"]
        name = self.store.customers.get(customer_id, {}).get("name", "")
        contact = next((c for c in (self.store.contacts.get(l["contact_id"]) for l in self.store.links_by_account
                        .get(account_id, [])) if c and c.get("mobile") == mobile[-10:]), None)
        email, source = resolve_email(self.settings, self.emails, mobile, contact and contact["contact_id"], name,
                                      customer_id)
        log.info("agent otp email from %s", source)
        request_id = f"agent:{uuid.uuid4().hex[:12]}"
        try:
            code = self.otp.issue(request_id)
        except Exception:                 # noqa: BLE001 - Redis down: no OTP, verification fails closed
            log.exception("agent OTP issue failed")
            return None
        try:
            self.email.send_otp(email, code, name)
        except Exception:                 # noqa: BLE001
            log.exception("agent OTP email failed")
        return request_id

    def verify(self, request_id: str, code: str) -> bool:
        try:
            status, _ = self.otp.verify(request_id, code)
        except Exception:                 # noqa: BLE001
            log.exception("agent OTP verify failed")
            return False
        return status == "ok"
