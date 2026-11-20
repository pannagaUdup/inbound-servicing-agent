"""A receipt for the borrower's OTHER loan is a wrong-loan pick, not a fake; and "I already paid / already
sent the UTR" right after a receipt is answered about that receipt, not with "share the UTR" again."""

from servicing_agent.agent.graph import new_message_id
from servicing_agent.contracts.state import InboundMessage, MediaEvidence

from tests.test_agent_flow import say
from tests.test_reverify_flow import _app, _bodies, _multi_loan_sender, _send, _verify_via_chooser

ASK_UTR = "Please share the 12-digit UTR / reference number"


def _paid_loans(rt, accounts):
    """Two of the sender's loans, the second with a successful payment."""
    for b in accounts:
        p = next((x for x in rt.store.payments_by_account.get(b, []) if x["status"] == "success"), None)
        if p:
            return next(a for a in accounts if a != b), b, p
    raise AssertionError("no paid loan")


def _verify_agent(rt, sender, account_id):
    say(rt, sender, account_id)
    return say(rt, sender, "123456")


def _image(rt, sender, text=None, **receipt):
    return rt.run_turn(InboundMessage(message_id=new_message_id(), sender=sender, type="image", text=text,
                                      media=MediaEvidence(kind="upi_receipt", provider="fake", **receipt)))


def test_receipt_for_the_borrowers_other_loan_is_not_treated_as_fake(runtime):
    sender, accounts = _multi_loan_sender(runtime)
    a, b, p = _paid_loans(runtime, accounts)
    assert _verify_agent(runtime, sender, a).verified
    r = _image(runtime, sender, utr=p["utr"], amount=p["amount"], txn_date=p["txn_date"])
    reply = r.replies[0]
    assert reply.template_id == "claim_other_own_loan" and not r.tickets             # no fraud ticket
    assert f"{b[:4]}••••{b[-4:]}" in reply.text and "SWITCH LOAN" in reply.text
    assert b not in reply.text and a not in reply.text                                # masked only
    assert "CLM-05" in r.normalized["policy_clauses"]


def test_someone_elses_utr_is_still_flagged(runtime):
    sender, accounts = _multi_loan_sender(runtime)
    a = accounts[0]
    _verify_agent(runtime, sender, a)
    stranger = next(p for p in runtime.store.payments if p["account_id"] and p["account_id"] not in accounts)
    r = _image(runtime, sender, utr=stranger["utr"], amount=stranger["amount"], txn_date=stranger["txn_date"])
    assert r.replies[0].template_id == "claim_confirm_receipt"
    r = say(runtime, sender, "yes")
    assert r.replies[0].template_id == "claim_confirmed_checking" and r.tickets[0]["queue"] == "compliance_review"


def test_followup_after_an_unverified_receipt_does_not_ask_for_the_utr_again(runtime, borrower):
    a, sender = borrower
    _verify_agent(runtime, sender, a["account_id"])
    p = next(p for p in runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    _image(runtime, sender, utr=p["utr"], amount=p["amount"] + 3000, txn_date=p["txn_date"])
    r = say(runtime, sender, "But I have already made payment, if you are unable to verify then what can I do")
    # Not confirmed yet: asked to double-check that receipt again, never asked for the UTR again.
    assert r.replies[0].template_id == "claim_confirm_receipt" and ASK_UTR not in r.replies[0].text
    assert not r.tickets and p["utr"][-4:] in r.replies[0].text
    r = say(runtime, sender, "I have already shared the UTR reference number to you")
    assert r.replies[0].template_id == "claim_confirm_receipt" and ASK_UTR not in r.replies[0].text


def test_followup_while_checking_and_after_confirmation(runtime, borrower):
    a, sender = borrower
    _verify_agent(runtime, sender, a["account_id"])
    r = _image(runtime, sender, utr="909090909090", amount=1500.0, txn_date="2026-11-20")
    assert r.replies[0].template_id == "claim_checking"
    assert say(runtime, sender, "I already paid, please check").replies[0].template_id == "claim_still_checking"

    p = next(p for p in runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    _image(runtime, sender, utr=p["utr"], amount=p["amount"], txn_date=p["txn_date"])
    assert say(runtime, sender, "I have paid").replies[0].template_id == "claim_already_confirmed"   # re-checked live


def test_no_recent_receipt_still_asks_for_the_utr(runtime, borrower):
    a, sender = borrower
    _verify_agent(runtime, sender, a["account_id"])
    assert ASK_UTR in say(runtime, sender, "I have paid").replies[0].text


def test_whatsapp_switch_loan_rechecks_the_receipt_on_the_right_loan(settings):
    app, rt = _app(settings)
    sender, accounts = _multi_loan_sender(rt)
    a, b, p = _paid_loans(rt, accounts)
    _send(app, sender, "what is my interest rate")
    _verify_via_chooser(app, rt, sender, a)                                  # verified on the wrong loan
    rt.channel.sent.clear()
    receipt = MediaEvidence(kind="upi_receipt", utr=p["utr"], amount=p["amount"], txn_date=p["txn_date"], provider="fake")
    _send(app, sender, "i have made this payment pls check", type="image", media=receipt)
    assert any("SWITCH LOAN" in m for m in _bodies(rt))

    rt.channel.sent.clear()
    _send(app, sender, "switch loan")
    assert "Which loan is this about?" in _bodies(rt)
    rt.channel.sent.clear()
    _verify_via_chooser(app, rt, sender, b)
    sent = _bodies(rt)
    assert any("Verified" in m for m in sent) and any("confirmed your payment" in m for m in sent)
    assert rt.conversations.load(rt.conversation_id_for(sender))["session"]["scope"]["account_id"] == b
