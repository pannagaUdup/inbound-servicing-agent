"""CLM-02 v2: "already paid" + a screenshot that isn't in our records is double-checked with the borrower first.

  not found          -> "We can't find this payment (...). Is this the right screenshot?"  nothing raised yet
  YES, looks genuine -> money sent but not received: the 2-minute window; reflected -> confirmed with the receipt,
                        still missing -> compliance ticket, category payment_not_received, borrower gets the reference
  YES, looks fake    -> (CLM-03, asked the same way) compliance ticket, category suspected_fake, neutral reply
  NO                 -> nothing raised; asked for the correct receipt / UTR
"""

from dataclasses import replace

import pytest

from tests.test_agent_flow import say, verify
from tests.test_claim_authenticity import NEUTRAL_FORBIDDEN, _fresh_receipt, _image, _post_payment


@pytest.fixture
def runtime(settings):
    from servicing_agent.platform.runtime import build_runtime
    return build_runtime(replace(settings, claim_confirm_missing=True))


def test_missing_payment_is_double_checked_before_anything_starts(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    rec = _fresh_receipt(1, "661122334455", 1500.0)
    r = _image(runtime, sender, **rec)
    reply = r.replies[0]
    assert reply.template_id == "claim_confirm_missing" and not r.tickets and not r.followups
    assert "can't find this payment" in reply.text and "4455" in reply.text and "right screenshot" in reply.text
    assert not any(w in reply.text.lower() for w in NEUTRAL_FORBIDDEN)


def test_yes_on_a_genuine_receipt_starts_the_window_then_escalates_as_not_received(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    _image(runtime, sender, **_fresh_receipt(1, "661122334466", 1500.0))
    r = say(runtime, sender, "yes")
    assert r.replies[0].template_id == "claim_checking" and not r.tickets
    f = r.followups[0]
    assert f["kind"] == "recheck_claim" and f["slots"]["confirmed"] is True

    late = runtime.recheck_claim(r.conversation_id, f["slots"])
    t = late.tickets[0]
    assert late.replies[0].template_id == "claim_failed_escalated" and t["ticket_id"] in late.replies[0].text
    assert t["request_type"] == "payment_unmatched" and t["queue"] == "compliance_review"
    assert t["summary"]["category"] == "payment_not_received" and t["summary"]["borrower_confirmed_screenshot"]


def test_yes_then_the_payment_reaches_us_is_confirmed(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    rec = _fresh_receipt(1, "661122334477", 1500.0)
    _image(runtime, sender, **rec)
    r = say(runtime, sender, "yes")
    _post_payment(runtime, a["account_id"], rec["utr"], rec["amount"], rec["txn_date"])
    late = runtime.recheck_claim(r.conversation_id, r.followups[0]["slots"])
    assert late.replies[0].template_id == "claim_matched" and not late.tickets


def test_payment_reaching_us_before_the_yes_is_confirmed_on_yes(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    rec = _fresh_receipt(1, "661122334488", 1500.0)
    _image(runtime, sender, **rec)
    _post_payment(runtime, a["account_id"], rec["utr"], rec["amount"], rec["txn_date"])
    r = say(runtime, sender, "yes")
    assert r.replies[0].template_id == "claim_matched" and not r.tickets and not r.followups


def test_no_raises_nothing_and_asks_for_the_right_receipt(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    _image(runtime, sender, **_fresh_receipt(1, "661122334499", 1500.0))
    r = say(runtime, sender, "no")
    assert r.replies[0].template_id == "claim_send_another" and not r.tickets and not r.followups


def test_fake_looking_receipt_not_in_records_is_routed_as_suspected_fake(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = _image(runtime, sender, utr="661122334400", amount=1234.0, txn_date="2026-11-01",
               payee_vpa="someone@okaxis", status="failed")
    assert r.replies[0].template_id == "claim_confirm_receipt" and not r.tickets      # double-check first
    r = say(runtime, sender, "yes")
    assert r.replies[0].template_id == "claim_confirmed_checking"
    assert not any(w in r.replies[0].text.lower() for w in NEUTRAL_FORBIDDEN)
    t = r.tickets[0]
    assert t["queue"] == "compliance_review" and t["summary"]["category"] == "suspected_fake"


def test_typed_utr_not_found_asks_about_the_utr(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = say(runtime, sender, "I already paid, UTR 661122334411")
    assert r.replies[0].template_id == "claim_confirm_missing" and "right UTR" in r.replies[0].text


def test_unreadable_image_asks_for_it_again(runtime, borrower):
    from servicing_agent.contracts.state import InboundMessage, MediaEvidence
    from servicing_agent.agent.graph import new_message_id
    a, sender = borrower
    verify(runtime, a, sender)
    r = runtime.run_turn(InboundMessage(message_id=new_message_id(), sender=sender, type="image",
                                        media=MediaEvidence(kind="other", confidence=0.0, error="docai_failed: timeout")))
    assert r.replies[0].template_id == "image_unreadable" and "couldn't read that image" in r.replies[0].text
