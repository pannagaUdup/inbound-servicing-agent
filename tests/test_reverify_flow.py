"""WhatsApp: a question that needs verification (never verified, or the session expired) goes through the
same loan chooser + emailed code as the menu, and the original question is answered afterwards."""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from servicing_agent.contracts.state import InboundMessage, MediaEvidence

from tests.test_inbound_flow import FakeOtp


def _app(settings):
    from servicing_agent.platform.api.app import create_app
    app = create_app(settings)
    TestClient(app)
    rt = app.state.runtime
    rt.inbound.otp = FakeOtp()
    return app, rt


def _multi_loan_sender(rt):
    by_mobile: dict[str, list[str]] = {}
    for acc, links in rt.store.links_by_account.items():
        for link in links:
            c = rt.store.contacts.get(link["contact_id"])
            if c and c.get("is_active"):
                by_mobile.setdefault(c["mobile"], []).append(acc)
    mobile, accs = next((m, a) for m, a in by_mobile.items() if len(set(a)) >= 2)
    return f"91{mobile}", sorted(set(accs))


_n = iter(range(10_000))


def _send(app, sender, text=None, type="text", media=None):
    app.state.process_batch([InboundMessage(message_id=f"w{next(_n)}", sender=sender, type=type, text=text,
                                            media=media)])


def _bodies(rt):
    out = []
    for m in rt.channel.sent:
        p = m["payload"]
        out.append((p.get("text") or {}).get("body") or ((p.get("interactive") or {}).get("body") or {}).get("text") or "")
    return out


def _verify_via_chooser(app, rt, sender, account_id):
    _send(app, sender, account_id, type="button")              # the borrower picks the loan from the list
    _send(app, sender, "424242")                               # emailed code


def test_question_before_verifying_uses_the_loan_chooser_then_answers_it(settings):
    app, rt = _app(settings)
    sender, accounts = _multi_loan_sender(rt)
    _send(app, sender, "what is my total outstanding amount")
    sent = _bodies(rt)
    assert "Which loan is this about?" in sent
    assert not any("share your loan account number" in b for b in sent)

    rt.channel.sent.clear()
    _verify_via_chooser(app, rt, sender, accounts[0])
    sent = _bodies(rt)
    assert any("Verified" in b for b in sent)
    assert any("principal outstanding" in b.lower() for b in sent)              # the original question
    assert not any("share your loan account number" in b for b in sent)


def test_expired_session_reverifies_through_the_chooser(settings):
    app, rt = _app(settings)
    sender, accounts = _multi_loan_sender(rt)
    _send(app, sender, "what is my interest rate")
    _verify_via_chooser(app, rt, sender, accounts[0])

    sess = rt.conversations.load(rt.conversation_id_for(sender))["session"]
    sess["scope"]["expires_at"] = (datetime.utcnow() - timedelta(minutes=1)).isoformat()   # session times out
    rt.channel.sent.clear()
    _send(app, sender, "what is my total outstanding amount")
    sent = _bodies(rt)
    assert any("session has ended" in b for b in sent) and "Which loan is this about?" in sent
    assert not any("share your loan account number" in b for b in sent)

    rt.channel.sent.clear()
    _verify_via_chooser(app, rt, sender, accounts[1])                        # may pick another loan
    assert any("principal outstanding" in b.lower() for b in _bodies(rt))
    assert rt.conversations.load(rt.conversation_id_for(sender))["session"]["scope"]["account_id"] == accounts[1]


def test_screenshot_before_verifying_is_checked_after_verification(settings):
    app, rt = _app(settings)
    sender, accounts = _multi_loan_sender(rt)
    acc = accounts[0]
    p = next((x for x in rt.store.payments_by_account.get(acc, []) if x["status"] == "success"), None)
    if p is None:
        import pytest
        pytest.skip("no successful payment on this loan")
    receipt = MediaEvidence(kind="upi_receipt", utr=p["utr"], amount=p["amount"], txn_date=p["txn_date"], provider="fake")
    _send(app, sender, "I have paid", type="image", media=receipt)
    assert "Which loan is this about?" in _bodies(rt)
    rt.channel.sent.clear()
    _verify_via_chooser(app, rt, sender, acc)
    assert any("confirmed your payment" in b for b in _bodies(rt))


def test_greeting_resets_a_pending_question(settings):
    app, rt = _app(settings)
    sender, accounts = _multi_loan_sender(rt)
    _send(app, sender, "what is my total outstanding amount")
    _send(app, sender, "hi")                                                   # starts over with the menu
    _send(app, sender, "ACCOUNT_STATUS", type="button")
    rt.channel.sent.clear()
    _verify_via_chooser(app, rt, sender, accounts[0])
    sent = _bodies(rt)
    assert sum("Your" in b and "loan" in b for b in sent) == 1                # menu pick served once, no replay
