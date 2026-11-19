"""Email OTP delivery (owner: T1). Phase 1.

No SMTP host configured -> MockEmailSender, which just logs the OTP (dev). Set
SA_SMTP_* to send for real. Customer emails are synthesized from the name until the
data carries real addresses.
"""

from __future__ import annotations

import logging
import re
import smtplib
from email.mime.text import MIMEText

from servicing_agent.config import Settings

logger = logging.getLogger(__name__)

OTP_SUBJECT = "Your CreditNirvana verification code"
OTP_BODY = ("Hello {name},\n\nYour one-time verification code is: {code}\n"
            "It is valid for 5 minutes. Please do not share it with anyone.\n\n"
            "— CreditNirvana")


def synthesize_email(name: str | None, customer_id: str, domain: str) -> str:
    base = re.sub(r"[^a-z0-9]+", ".", (name or customer_id).lower()).strip(".") or customer_id.lower()
    return f"{base}@{domain}"


def mask_email(addr: str) -> str:
    local, _, dom = addr.partition("@")
    shown = local[0] if local else "*"
    return f"{shown}{'*' * max(1, len(local) - 1)}@{dom}"


class MockEmailSender:
    """Logs the email instead of sending. Dev default; proves the flow without SMTP."""

    def send_otp(self, to_email: str, code: str, name: str = "") -> None:
        logger.info("EMAIL(mock) OTP -> %s | code=%s | subject=%r", to_email, code, OTP_SUBJECT)


class SmtpEmailSender:
    def __init__(self, settings: Settings):
        self.s = settings

    def send_otp(self, to_email: str, code: str, name: str = "") -> None:
        msg = MIMEText(OTP_BODY.format(name=name or "there", code=code))
        msg["Subject"] = OTP_SUBJECT
        msg["From"] = self.s.email_from
        msg["To"] = to_email
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=10) as server:
            if self.s.smtp_tls:
                server.starttls()
            if self.s.smtp_user:
                server.login(self.s.smtp_user, self.s.smtp_password)
            server.send_message(msg)
        logger.info("EMAIL(smtp) OTP sent -> %s", to_email)


def make_email_sender(settings: Settings):
    return SmtpEmailSender(settings) if settings.smtp_host else MockEmailSender()
