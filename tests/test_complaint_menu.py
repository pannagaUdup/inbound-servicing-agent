"""'Raise a complaint' menu: verified-gated capture -> auto-resolve informational (YES/NO) or escalate."""

import uuid

from servicing_agent.contracts.state import InboundMessage


def _texts(ch):
    out = []
    for m in ch.sent:
        b = (m.get("payload", {}).get("text") or {}).get("body")
        if b:
            out.append(b)
    return out


def _verify_session(runtime, sender, account_id):
    cid = runtime.inbound._cid(sender)
    convo = runtime.conversations.load(cid)
    convo["session"]["scope"] = {"account_id": account_id, "customer_id": "c", "contact_id": "ct", "method": "otp"}
    convo["session"]["inbound"] = {"stage": "menu"}       # as if the greeting menu was shown
    runtime.conversations.persist(cid)


def _msg(sender, text, typ="text"):
    return InboundMessage(message_id="m" + uuid.uuid4().hex[:8], sender=sender, type=typ, text=text)


def test_menu_prompts_for_complaint_when_verified(runtime, borrower):
    acc, sender = borrower
    _verify_session(runtime, sender, acc["account_id"])
    r = runtime.inbound.handle(_msg(sender, "RAISE_COMPLAINT", "button"))
    assert r and r.get("handled")
    assert any("describe your complaint" in t.lower() for t in _texts(runtime.channel))


def test_informational_complaint_autoresolves_then_yes_logs_resolved(runtime, borrower):
    acc, sender = borrower
    _verify_session(runtime, sender, acc["account_id"])
    runtime.inbound.handle(_msg(sender, "RAISE_COMPLAINT", "button"))
    runtime.inbound.handle(_msg(sender, "why are my charges so high, please explain the charges"))
    assert any("Did this resolve" in t for t in _texts(runtime.channel))   # auto-answer + confirm
    runtime.inbound.handle(_msg(sender, "yes"))
    assert any("resolved" in t.lower() for t in _texts(runtime.channel))
    resolved = [t for t in runtime.tools.tickets.list()
                if t["request_type"] == "complaint" and t["status"] == "resolved"]
    assert resolved                                                        # a resolved complaint record exists


def test_informational_complaint_no_escalates_to_human(runtime, borrower):
    acc, sender = borrower
    _verify_session(runtime, sender, acc["account_id"])
    runtime.inbound.handle(_msg(sender, "RAISE_COMPLAINT", "button"))
    runtime.inbound.handle(_msg(sender, "what are my charges, explain please"))
    runtime.inbound.handle(_msg(sender, "no"))
    opens = runtime.tools.tickets.list(status="open")
    assert any(t["request_type"] == "complaint" for t in opens)


def test_harassment_complaint_always_escalates(runtime, borrower):
    acc, sender = borrower
    _verify_session(runtime, sender, acc["account_id"])
    runtime.inbound.handle(_msg(sender, "RAISE_COMPLAINT", "button"))
    runtime.inbound.handle(_msg(sender, "your recovery agent is harassing my family and threatening us"))
    opens = runtime.tools.tickets.list(status="open")
    assert any(t["request_type"] == "harassment_complaint" for t in opens)
    assert not any("Did this resolve" in t for t in _texts(runtime.channel))   # no auto-resolve for harassment


def test_complaint_menu_requires_verification(runtime):
    sender = "919876500001"                                 # unverified session
    runtime.inbound.handle(_msg(sender, "hi"))
    runtime.inbound.handle(_msg(sender, "RAISE_COMPLAINT", "button"))
    assert not any("describe your complaint" in t.lower() for t in _texts(runtime.channel))
