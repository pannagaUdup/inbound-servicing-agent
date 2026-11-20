"""The 2026-11-20 WhatsApp conversation: outstanding, can't pay in full, "tell me", "I can only pay 5000"."""

from servicing_agent.agent.handlers import is_followup
from servicing_agent.agent.planner import RulePlanner
from servicing_agent.contracts.state import Intent
from tests.test_agent_flow import say, verify


def test_needs_to_be_paid_is_a_question_not_a_payment_claim():
    intents = [t.intent for t in RulePlanner().plan("What is my total outstanding which needs to be paid?")]
    assert Intent.PAYMENT_CLAIM not in intents and Intent.LOAN_SUMMARY in intents
    for claim in ("I have paid", "already paid the emi", "paid 5000 yesterday"):
        assert Intent.PAYMENT_CLAIM in [t.intent for t in RulePlanner().plan(claim)], claim


def test_followup_phrases():
    for t in ("tell me", "Tell me more", "explain", "ok explain that", "what does that mean?", "details please"):
        assert is_followup(t), t
    for t in ("tell me my emi", "send my statement", "what is my interest rate"):
        assert not is_followup(t), t


def _difficulty(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = say(runtime, sender, "What if I won't be able to make the full payment on time?")
    assert r.replies[0].template_id == "payment_difficulty"
    return a, sender


def test_tell_me_explains_the_options_policy(runtime, borrower):
    a, sender = _difficulty(runtime, borrower)
    r = say(runtime, sender, "tell me")
    assert r.replies[0].template_id == "policy_explain"
    text = r.replies[0].text
    assert "supervisor" in text and ("credit" in text or "settled" in text) and "WAIVER" in text
    assert not r.tickets


def test_tell_me_without_context_is_still_out_of_scope(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    say(runtime, sender, "what is my emi")
    assert say(runtime, sender, "tell me").replies[0].template_id != "policy_explain"


def test_an_amount_gets_a_link_the_shortfall_and_the_waiver_settlement_offer(runtime, borrower):
    a, sender = _difficulty(runtime, borrower)
    acc = runtime.store.accounts[a["account_id"]]
    due = float(acc["overdue_amount"] or 0) or float(acc["emi_amount"])
    pay = round(due / 2, 2)
    r = say(runtime, sender, f"I can only pay ₹{pay:.2f} max.")
    reply = r.replies[0]
    assert reply.template_id == "payment_difficulty_partial"
    assert f"{pay:,.2f}" in reply.text and f"{round(due - pay, 2):,.2f}" in reply.text and "SETTLE" in reply.text
    r = say(runtime, sender, "SETTLE")                                   # offer = what they can pay; reason?
    assert r.replies[0].template_id == "case_details_needed" and "reason" in r.replies[0].text
    assert "one-time settlement" not in r.replies[0].text                 # the amount is already known
    r = say(runtime, sender, "I lost my job")
    confirm = r.replies[-1]
    assert confirm.template_id == "case_confirm" and "credit" in confirm.text.lower()
    assert f"you pay ₹{pay:,.2f}" in confirm.text and "job loss" in confirm.text
    assert runtime.cases.open_case_for_account(a["account_id"]) is None    # nothing submitted before YES


def test_cant_pay_is_triaged_as_compliance(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = say(runtime, sender, "I won't be able to pay, I don't have much money")
    assert r.normalized["triage"]["category"] == "compliance" and r.normalized["triage"]["hardship"]


def test_why_does_it_need_approval_is_answered_not_raised(runtime, borrower):
    a, sender = _difficulty(runtime, borrower)
    r = say(runtime, sender, "Why does the waiver and settlement need approval?")
    assert r.replies[0].template_id == "policy_answer" and not r.tickets
    assert "supervisor" in r.replies[0].text
    r = say(runtime, sender, "Yes, I would like to request a settlement.")          # the screenshot's next turn
    assert r.replies[-1].template_id == "case_details_needed"                       # amount and reason first
    assert runtime.cases.open_case_for_account(a["account_id"]) is None


def test_details_question_then_answers_then_yes(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = say(runtime, sender, "I want a settlement")
    ask = r.replies[-1].text
    assert "one-time settlement" in ask and "reason" in ask
    r = say(runtime, sender, "yes")                                       # no details: asked again, not submitted
    assert r.replies[-1].template_id == "case_details_needed" and not r.tickets
    r = say(runtime, sender, "8000 because of medical treatment")
    assert r.replies[-1].template_id == "case_confirm" and "you pay ₹8,000.00" in r.replies[-1].text
    r = say(runtime, sender, "yes")
    case = runtime.cases.open_case_for_account(a["account_id"])
    assert r.replies[-1].template_id == "waiver_created" and case.request.amount == 8000
    assert case.request.reason_category == "medical"


def test_two_unanswered_asks_raise_nothing(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    say(runtime, sender, "I want a settlement")
    say(runtime, sender, "yes")
    r = say(runtime, sender, "ok")
    assert r.replies[-1].template_id == "case_details_incomplete" and not r.tickets
    assert runtime.cases.open_case_for_account(a["account_id"]) is None


def test_another_question_lapses_the_details_request(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    say(runtime, sender, "I want a settlement")
    r = say(runtime, sender, "send my statement")
    assert r.replies[0].template_id == "statement_sent"
    assert say(runtime, sender, "5000, I lost my job").replies[-1].template_id != "case_confirm"
