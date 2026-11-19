"""Waiver / settlement requests sent with a payment screenshot: matched-full, matched-partial, not matched."""

from tests.test_agent_flow import say, verify
from tests.test_claim_authenticity import _image, _post_payment


def _dues(runtime, account_id):
    a = runtime.store.accounts[account_id]
    return float(a["overdue_amount"] or 0) or float(a["emi_amount"])


def _setup(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    return a, sender, _dues(runtime, a["account_id"])


def _with_receipt(runtime, sender, text, **receipt):
    """One turn: the request text plus the screenshot (as the batcher merges them)."""
    from servicing_agent.agent.graph import new_message_id
    from servicing_agent.contracts.state import InboundMessage, MediaEvidence
    return runtime.run_turn(InboundMessage(message_id=new_message_id(), sender=sender, type="image", text=text,
                                           media=MediaEvidence(kind="upi_receipt", provider="fake", **receipt)))


def test_full_payment_needs_no_waiver(runtime, borrower):
    a, sender, due = _setup(runtime, borrower)
    _post_payment(runtime, a["account_id"], "700000000001", due, "2026-11-19")
    r = _with_receipt(runtime, sender, "I have paid, please waive the penalty",
                      utr="700000000001", amount=due, txn_date="2026-11-19")
    assert [x.template_id for x in r.replies] == ["claim_matched", "case_not_needed"]
    assert "nothing left to waive" in r.replies[1].text and not r.tickets
    assert runtime.cases.open_case_for_account(a["account_id"]) is None
    assert say(runtime, sender, "yes").replies[0].template_id != "waiver_created"        # nothing pending


def test_partial_payment_explains_consequences_then_goes_to_approval_and_compliance(runtime, borrower):
    a, sender, due = _setup(runtime, borrower)
    paid = round(due / 2, 2)
    _post_payment(runtime, a["account_id"], "700000000002", paid, "2026-11-19")
    r = _with_receipt(runtime, sender, "I paid this much, please waive the rest",
                      utr="700000000002", amount=paid, txn_date="2026-11-19")
    assert [x.template_id for x in r.replies] == ["claim_matched", "case_confirm_partial"]
    ask = r.replies[1].text
    shortfall = round(due - paid, 2)
    assert f"{shortfall:,.2f}" in ask and "supervisor" in ask and "rejected" in ask and "YES" in ask
    assert runtime.cases.open_case_for_account(a["account_id"]) is None                  # nothing yet

    yes = say(runtime, sender, "yes")
    assert yes.replies[0].template_id == "waiver_created"
    queues = [t["queue"] for t in yes.tickets]
    assert queues == ["waiver_approvals", "compliance_review"]
    assert yes.tickets[1]["summary"]["partial_payment"]["shortfall"] == shortfall
    case = runtime.cases.open_case_for_account(a["account_id"])
    assert case.request.amount == shortfall


def test_partial_payment_keeps_the_amount_the_borrower_asked_for(runtime, borrower):
    a, sender, due = _setup(runtime, borrower)
    paid = round(due / 2, 2)
    _post_payment(runtime, a["account_id"], "700000000003", paid, "2026-11-19")
    _with_receipt(runtime, sender, "please waive Rs 500 of penal charges",
                  utr="700000000003", amount=paid, txn_date="2026-11-19")
    say(runtime, sender, "yes")
    assert runtime.cases.open_case_for_account(a["account_id"]).request.amount == 500


def test_partial_settlement_mentions_the_credit_report(runtime, borrower):
    a, sender, due = _setup(runtime, borrower)
    paid = round(due / 3, 2)
    _post_payment(runtime, a["account_id"], "700000000004", paid, "2026-11-19")
    r = _with_receipt(runtime, sender, "I want to settle the remaining", utr="700000000004", amount=paid,
                      txn_date="2026-11-19")
    assert r.replies[-1].template_id == "case_confirm_partial" and "credit" in r.replies[-1].text


def test_partial_payment_borrower_says_no(runtime, borrower):
    a, sender, due = _setup(runtime, borrower)
    paid = round(due / 2, 2)
    _post_payment(runtime, a["account_id"], "700000000005", paid, "2026-11-19")
    _with_receipt(runtime, sender, "waive the rest", utr="700000000005", amount=paid, txn_date="2026-11-19")
    r = say(runtime, sender, "no")
    assert r.replies[0].template_id == "case_cancelled" and not r.tickets
    assert runtime.cases.open_case_for_account(a["account_id"]) is None


def test_fake_screenshot_with_a_waiver_request_raises_nothing_visible(runtime, borrower):
    a, sender, due = _setup(runtime, borrower)
    p = next(p for p in runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    r = _with_receipt(runtime, sender, "paid, please waive the rest", utr=p["utr"], amount=p["amount"] + 5000,
                      txn_date=p["txn_date"])
    assert [x.template_id for x in r.replies] == ["claim_unverified", "case_waits_for_payment"]
    assert [t["queue"] for t in r.tickets] == ["fraud_review"]                           # silent
    assert runtime.cases.open_case_for_account(a["account_id"]) is None


def test_unposted_payment_with_a_waiver_request_waits(runtime, borrower):
    a, sender, due = _setup(runtime, borrower)
    r = _with_receipt(runtime, sender, "paid, please waive the rest", utr="700000000099", amount=100.0,
                      txn_date="2026-11-20")
    assert [x.template_id for x in r.replies] == ["claim_checking", "case_waits_for_payment"]
    assert runtime.cases.open_case_for_account(a["account_id"]) is None


def test_waiver_without_screenshot_explains_consequences(runtime, borrower):
    a, sender, due = _setup(runtime, borrower)
    r = say(runtime, sender, "please waive my penal charges of Rs 1000")
    text = r.replies[0].text
    assert r.replies[0].template_id == "case_confirm" and "rejected" in text and "supervisor" in text
    assert "credit" not in text                                                          # waiver, not settlement
