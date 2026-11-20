"""'I can't pay the full amount this month': acknowledged with the options, not out of scope, and not a
settlement opened on the borrower's behalf."""

import pytest

from servicing_agent.agent.planner import FallbackPlanner, RulePlanner, SarvamIntentClassifier
from servicing_agent.agent.triage import triage
from servicing_agent.contracts.state import Intent, Task

from tests.test_agent_flow import say, verify


@pytest.mark.parametrize("text", [
    "I cannot pay the full amount now.",                       # the two messages from WhatsApp
    "I won't be able to pay the full amount this month.",
    "i wont be able to make full payment this month",
    "i cant pay full emi this month",
    "can I pay half this month",
    "I need more time to pay",
    "paise nahi hai is mahine",
])
def test_cant_pay_in_full_is_payment_difficulty(text):
    assert [t.intent for t in RulePlanner().plan(text)] == [Intent.PAYMENT_DIFFICULTY]


@pytest.mark.parametrize("text,expected", [
    ("I lost my job, I cannot pay, please waive the penalty", Intent.WAIVER_REQUEST),     # explicit ask wins
    ("I cannot afford the full amount, I can pay half to close", Intent.SETTLEMENT_REQUEST),
    ("WAIVER", Intent.WAIVER_REQUEST),
    ("SETTLE", Intent.SETTLEMENT_REQUEST),
])
def test_explicit_choice_goes_to_the_request_flow(text, expected):
    assert [t.intent for t in RulePlanner().plan(text)] == [expected]


def test_hardship_is_flagged():
    t = triage("I won't be able to make the full payment this month", [Task(intent=Intent.PAYMENT_DIFFICULTY)])
    assert t.hardship and t.sentiment == "distressed"


def test_classifier_can_label_it_when_rules_miss():
    class Fake:
        def chat(self, messages, **kw):
            assert "payment_difficulty" in messages[0]["content"]       # the label exists in the guide
            return "payment_difficulty"
    planner = FallbackPlanner(RulePlanner(), SarvamIntentClassifier(Fake(), ["m"]))
    assert [t.intent for t in planner.plan("money is tight, salary delayed")] == [Intent.PAYMENT_DIFFICULTY]


def test_end_to_end_options_then_choice(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = say(runtime, sender, "I won't be able to pay the full amount this month.")
    reply = r.replies[0]
    assert reply.template_id == "payment_difficulty" and not r.tickets
    assert "WAIVER" in reply.text and "SETTLE" in reply.text and "approval" in reply.text
    assert runtime.cases.open_case_for_account(a["account_id"]) is None              # nothing opened
    assert "HRD-02" in r.normalized["policy_clauses"]

    r = say(runtime, sender, "SETTLE")
    assert r.replies[-1].template_id == "case_details_needed"                       # amount + reason first
    r = say(runtime, sender, "₹5000, my salary was delayed")
    assert r.replies[-1].template_id == "case_confirm" and "credit" in r.replies[-1].text   # STL-01 explained
    assert "you pay ₹5,000.00" in r.replies[-1].text and "salary delay" in r.replies[-1].text
    assert runtime.cases.open_case_for_account(a["account_id"]) is None              # still needs YES


def test_part_payment_link_for_the_amount_named(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    say(runtime, sender, "I cannot pay the full amount now.")
    r = say(runtime, sender, "I can pay 5000 now")
    assert r.replies[0].template_id == "pay_now" and "5,000.00" in r.replies[0].text
