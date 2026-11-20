"""Where a verification code is emailed: looked up from the contacts table by the sender's mobile number.

Order: the email on the contact (``contacts.email``, matched on ``mobile_hash``; the contact verified for the
loan first) -> ``SA_OTP_EMAIL_OVERRIDE`` (dev: one inbox for numbers without an email) -> an address made up
from the customer's name (mock only; nobody receives it).

Set or check an email for a number:

    .venv/bin/python -m servicing_agent.platform.contact_email --mobile 8867635479 --email you@example.com
    .venv/bin/python -m servicing_agent.platform.contact_email --mobile 8867635479
"""

from __future__ import annotations

import argparse
import logging
import re

from sqlalchemy import select, update

from servicing_agent.platform.campaigns import mobile_hash
from servicing_agent.platform.db import Contact
from servicing_agent.platform.email import synthesize_email

log = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ContactEmails:
    def __init__(self, session_factory):
        self.Session = session_factory

    def email_for(self, mobile: str, contact_id: str | None = None) -> str | None:
        """The email on file for this mobile number (the given contact's first), or None."""
        try:
            with self.Session() as s:
                rows = s.execute(select(Contact.contact_id, Contact.email).where(
                    Contact.mobile_hash == mobile_hash(mobile), Contact.email.is_not(None))).all()
        except Exception:  # noqa: BLE001 - no DB / no column yet: fall back
            log.exception("contact email lookup failed")
            return None
        rows = [r for r in rows if r.email]
        if not rows:
            return None
        return next((r.email for r in rows if r.contact_id == contact_id), rows[0].email)

    def set(self, mobile: str, email: str | None) -> int:
        """Set (or clear, with None) the email on every contact row with this mobile. Returns rows updated."""
        if email is not None and not EMAIL_RE.match(email):
            raise ValueError(f"not an email address: {email!r}")
        with self.Session.begin() as s:
            return s.execute(update(Contact).where(Contact.mobile_hash == mobile_hash(mobile))
                             .values(email=email)).rowcount


def resolve_email(settings, directory: "ContactEmails | None", mobile: str, contact_id: str | None,
                  name: str, customer_id: str) -> tuple[str, str]:
    """(email, source) where source is contact | override | synthesized."""
    if directory is not None and mobile:
        found = directory.email_for(mobile, contact_id)
        if found:
            return found, "contact"
    if settings.otp_email_override:
        return settings.otp_email_override, "override"
    return synthesize_email(name, customer_id, settings.email_domain), "synthesized"


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from servicing_agent.config import get_settings
    from servicing_agent.platform.db import make_session_factory
    load_dotenv(".env")
    ap = argparse.ArgumentParser(description="Set or show the email used for a mobile number's verification codes")
    ap.add_argument("--mobile", required=True)
    ap.add_argument("--email", help="email to set; omit to show the current one; 'none' to clear")
    a = ap.parse_args(argv)
    d = ContactEmails(make_session_factory(get_settings().database_url))
    if a.email:
        n = d.set(a.mobile, None if a.email.lower() == "none" else a.email)
        print(f"updated {n} contact row(s)" if n else "no contact with that mobile number")
        if not n:
            return 1
    print(f"{a.mobile}: {d.email_for(a.mobile) or '(no email on file; the override or a made-up address is used)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
