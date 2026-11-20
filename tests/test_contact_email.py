"""Verification codes go to the email on file for the sender's mobile (contacts table), else the override."""

from dataclasses import replace

import pytest

from servicing_agent.platform.contact_email import ContactEmails, resolve_email
from servicing_agent.platform.db import make_session_factory


@pytest.fixture
def emails(tmp_path, settings):
    from servicing_agent.data.db_loader import load
    url = f"sqlite:///{tmp_path}/c.db"
    load(settings.data_dir, url)
    return ContactEmails(make_session_factory(url)), url


def _a_mobile(settings):
    import csv
    row = next(csv.DictReader(open(settings.data_dir / "contacts.csv")))
    return row["mobile"], row["contact_id"]


def test_email_on_file_wins_over_the_override(emails, settings):
    d, _ = emails
    mobile, contact_id = _a_mobile(settings)
    s = replace(settings, otp_email_override="team@example.com")
    assert resolve_email(s, d, "91" + mobile, contact_id, "A B", "C1") == ("team@example.com", "override")
    assert d.set(mobile, "me@example.com") >= 1
    assert resolve_email(s, d, "91" + mobile, contact_id, "A B", "C1") == ("me@example.com", "contact")
    assert d.email_for("+91 " + mobile) == "me@example.com"                    # any mobile format


def test_without_email_or_override_a_made_up_address_is_used(emails, settings):
    d, _ = emails
    mobile, contact_id = _a_mobile(settings)
    email, source = resolve_email(replace(settings, otp_email_override=""), d, mobile, contact_id, "Asha Rao", "C1")
    assert source == "synthesized" and email.startswith("asha.rao@")


def test_invalid_email_is_rejected_and_clear_works(emails, settings):
    d, _ = emails
    mobile, _ = _a_mobile(settings)
    with pytest.raises(ValueError):
        d.set(mobile, "not-an-email")
    d.set(mobile, "me@example.com")
    d.set(mobile, None)
    assert d.email_for(mobile) is None


def test_emails_survive_a_data_reload(emails, settings):
    from servicing_agent.data.db_loader import load
    d, url = emails
    mobile, _ = _a_mobile(settings)
    d.set(mobile, "me@example.com")
    load(settings.data_dir, url)
    assert d.email_for(mobile) == "me@example.com"


def test_whatsapp_menu_otp_goes_to_the_email_on_file(settings, monkeypatch):
    from servicing_agent.data.db_loader import load
    from tests.test_reverify_flow import _app, _multi_loan_sender, _send
    load(settings.data_dir, settings.database_url)                        # contacts in the DB, as in prod
    app, rt = _app(settings)
    sender, accounts = _multi_loan_sender(rt)
    assert rt.contact_emails.set(sender, "me@example.com") >= 1
    sent = []
    monkeypatch.setattr(rt.inbound.email, "send_otp", lambda to, code, name="": sent.append(to))
    _send(app, sender, "hi")
    _send(app, sender, "what is my interest rate")
    _send(app, sender, accounts[0]) if not sent else None
    assert sent and sent[-1] == "me@example.com"
