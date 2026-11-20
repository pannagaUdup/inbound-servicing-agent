"""Compliance / sentiment triage and the approval-process message for waivers and settlements."""

import pytest

from servicing_agent.agent.planner import RulePlanner
from servicing_agent.agent.triage import triage
from servicing_agent.contracts.state import Intent, Task

from tests.test_account_details import client_for


@pytest.mark.parametrize("text,expected", [
    ("I lost my job, can you reduce the amount I owe", Intent.SETTLEMENT_REQUEST),
    ("can I get a discount to close the loan", Intent.SETTLEMENT_REQUEST),
    ("I cannot afford the full amount, I can pay half to close", Intent.SETTLEMENT_REQUEST),
    ("please reduce my penalty charges", Intent.WAIVER_REQUEST),
    ("don't charge me late fee this month", Intent.WAIVER_REQUEST),
])
def test_indirect_waiver_and_settlement_asks_are_detected(text, expected):
    assert [t.intent for t in RulePlanner().plan(text)] == [expected]


def test_routine_questions_stay_routine():
    for text in ("what is my outstanding amount", "why was i charged bounce charges", "reverse the extra charge"):
        assert Intent.WAIVER_REQUEST not in [t.intent for t in RulePlanner().plan(text)]
    t = triage("what is my emi", [Task(intent=Intent.EMI_DUE_QUERY)])
    assert (t.category, t.sentiment, t.priority, t.needs_approval) == ("routine", "neutral", "normal", False)


def test_triage_categories_and_sentiment():
    t = triage("I lost my job, please waive the penalty", [Task(intent=Intent.WAIVER_REQUEST)])
    assert (t.category, t.sentiment, t.hardship, t.needs_approval, t.priority) == \
        ("compliance", "distressed", True, True, "high")
    t = triage("worst service, fed up", [Task(intent=Intent.COMPLAINT)])
    assert (t.category, t.sentiment, t.priority) == ("compliance", "frustrated", "high")
    assert triage("thanks!", [Task(intent=Intent.GREETING)]).sentiment == "positive"


def test_waiver_reply_explains_supervisor_then_compliance(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    r = say("please reduce my penalty charges, my salary was delayed")
    ask = r["replies"][-1]
    assert ask["template_id"] == "case_confirm" and not r["tickets"]                 # asks first
    assert "supervisor" in ask["text"] and "compliance" in ask["text"] and "YES" in ask["text"]
    assert r["normalized"]["triage"]["needs_approval"] is True
    assert any(e["type"] == "triage" for e in r["events"])
    r = say("yes")
    text = r["replies"][-1]["text"]
    assert r["replies"][-1]["template_id"] == "waiver_created" and "Nothing has been decided" in text
    assert r["tickets"][0]["summary"]["triage"]["category"] == "compliance"
    assert r["tickets"][0]["summary"]["borrower_confirmed"] is True


def test_borrower_can_cancel_a_waiver_before_submitting(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    say("please waive my penal charges, I was in hospital")
    r = say("no")
    assert r["replies"][0]["template_id"] == "case_cancelled" and not r["tickets"]
    assert rt.cases.open_case_for_account(a["account_id"]) is None


def test_hardship_settlement_is_acknowledged_and_prioritised(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    r = say("I lost my job, can you reduce the amount I owe")
    assert [x["template_id"] for x in r["replies"]] == ["hardship_ack", "case_details_needed"]
    ask = r["replies"][-1]
    assert "one-time settlement" in ask["text"] and "reason" not in ask["text"]   # amount first; reason given
    r = say("I can pay 20000")
    assert [x["template_id"] for x in r["replies"]] == ["case_confirm"]
    assert "20,000" in r["replies"][-1]["text"]
    r = say("yes")
    assert r["tickets"][0]["summary"]["triage"]["priority"] == "high"


def test_status_says_which_approval_step(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    say("please waive my penal charges, I was in hospital")
    say("yes")
    r = say("any update on my request")
    assert "supervisor" in r["replies"][0]["text"] and "step 1 of 2" in r["replies"][0]["text"]
