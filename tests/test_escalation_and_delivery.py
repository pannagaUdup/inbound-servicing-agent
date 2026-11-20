"""'Escalate this to the compliance team' is a request, not out of scope; and every reply's delivery
(including the 2-minute escalation notice) is logged, retried once and audited."""

import time

import pytest

from servicing_agent.agent.planner import RulePlanner
from servicing_agent.contracts.state import Intent

from tests.test_agent_flow import say, verify
from tests.test_claim_authenticity import _fresh_receipt, _image

VOICE = "Uh, this discrepancy is causing too much of problems. Can you escalate this one to the compliance team?"


@pytest.mark.parametrize("text", [VOICE, "I want to speak to a manager", "please raise this to your manager",
                                  "can I talk to a real person", "escalate this"])
def test_escalation_is_recognised(text):
    assert [t.intent for t in RulePlanner().plan(text)] == [Intent.ESCALATION_REQUEST]


def test_escalation_links_the_payment_and_is_not_repeated(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = _image(runtime, sender, **_fresh_receipt(1, "123456789876", 1500.0))
    late = runtime.recheck_claim(r.conversation_id, r.followups[0]["slots"])          # 2 minutes later
    review = late.tickets[0]["ticket_id"]
    r = say(runtime, sender, VOICE)
    reply = r.replies[0]
    assert reply.template_id == "escalation_raised" and r.tickets[0]["queue"] == "compliance_review"
    assert r.tickets[0]["ticket_id"] in reply.text and review in reply.text and "9876" in reply.text
    assert r.tickets[0]["summary"]["payment"]["linked_ticket"] == review
    assert r.normalized["triage"]["category"] == "compliance"
    again = say(runtime, sender, "please escalate this")
    assert again.replies[0].template_id == "escalation_existing" and not again.tickets


def test_escalation_after_a_silent_fraud_flag_does_not_reveal_it(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    p = next(p for p in runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    fraud = _image(runtime, sender, utr=p["utr"], amount=p["amount"] + 3000, txn_date=p["txn_date"]).tickets[0]
    r = say(runtime, sender, "escalate this to the compliance team")
    assert r.replies[0].template_id == "escalation_raised" and fraud["ticket_id"] not in r.replies[0].text
    assert r.tickets[0]["summary"]["earlier_review_silent"] is True


def test_escalation_without_a_payment(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = say(runtime, sender, "I want to speak to a manager")
    assert r.replies[0].template_id == "escalation_raised" and "about your payment" not in r.replies[0].text


# ---------------- delivery

def _worker_app(settings, borrower, window=1):
    from dataclasses import replace
    from tests.test_worker_pipeline import FakePerception, setup
    rec = _fresh_receipt(1, "554433221100", 1500.0)
    app, rt, sender = setup(replace(settings, claim_window_seconds=window), borrower,
                            FakePerception(receipts={b"IMG": dict(rec)}), {"M1": b"IMG"})
    audited = []
    rt.audit_sink = lambda cid, events: audited.extend(events)
    return app, rt, sender, audited


def test_two_minute_notice_is_sent_and_audited(settings, borrower):
    from tests.test_worker_pipeline import msg
    app, rt, sender, audited = _worker_app(settings, borrower)
    app.state.process_batch([msg("w1", sender, type="image", media_id="M1")])
    deadline = time.time() + 4
    while time.time() < deadline and not any(e["type"] == "delivery" and e["data"]["kind"] == "recheck" for e in audited):
        time.sleep(0.05)
    deliveries = [e["data"] for e in audited if e["type"] == "delivery"]
    assert [d["template_id"] for d in deliveries] == ["claim_checking", "claim_failed_escalated"]
    assert all(d["ok"] for d in deliveries) and deliveries[1]["kind"] == "recheck"


def test_failed_send_is_retried_and_recorded(settings, borrower):
    from tests.test_worker_pipeline import msg
    app, rt, sender, audited = _worker_app(settings, borrower, window=60)
    real, calls = rt.channel.send_text, []

    def flaky(to, text):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("network blip")
        return real(to, text)
    rt.channel.send_text = flaky
    app.state.process_batch([msg("w1", sender, text="what is my emi")])
    d = [e["data"] for e in audited if e["type"] == "delivery"][0]
    assert len(calls) == 2 and d["ok"] is True                                       # retried, delivered
