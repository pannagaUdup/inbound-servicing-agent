"""Typos, free-form loan questions, internal info and off-topic messages (real WhatsApp, Nov 2026)."""

import pytest

from servicing_agent.agent.answerer import SarvamAnswerer
from servicing_agent.agent.planner import FallbackPlanner, RulePlanner, SarvamIntentClassifier, correct_spelling
from servicing_agent.contracts.state import Intent

from tests.test_account_details import client_for, no_guard_blocks


@pytest.mark.parametrize("text,expected", [
    ("what is the intrst rate", [Intent.LOAN_SUMMARY]),
    ("what is the collctr nme", [Intent.RESTRICTED_INFO]),
    ("what is the collector name of the account", [Intent.RESTRICTED_INFO]),
    ("what is my cibil score", [Intent.RESTRICTED_INFO]),
    ("when does my loan end", [Intent.ACCOUNT_QUESTION]),
    ("what was my loan amount", [Intent.ACCOUNT_QUESTION]),
    ("how many emis have i paid", [Intent.ACCOUNT_QUESTION]),       # a question, not a payment claim
    ("send my statment", [Intent.STATEMENT_REQUEST]),
    ("my loan is almost paid off, what is left?", [Intent.LOAN_SUMMARY]),
    ("i have paid", [Intent.PAYMENT_CLAIM]),
])
def test_rules_handle_typos_and_new_questions(text, expected):
    assert [t.intent for t in RulePlanner().plan(text)] == expected


def test_spelling_correction_leaves_normal_words_alone():
    assert correct_spelling("what is the intrst rate") == "what is the interest rate"
    assert correct_spelling("why was I charged") == "why was I charged"


class FakeSarvam:
    def __init__(self, reply="", fail=False):
        self.reply, self.fail, self.calls = reply, fail, 0

    def chat(self, messages, *, model, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return self.reply


def test_classifier_labels_what_the_rules_miss():
    planner = FallbackPlanner(RulePlanner(), SarvamIntentClassifier(FakeSarvam("out_of_scope"), ["m"]))
    assert [t.intent for t in planner.plan("will it rain tomorrow")] == [Intent.OUT_OF_SCOPE]


def test_classifier_not_called_when_rules_match():
    fake = FakeSarvam("out_of_scope")
    planner = FallbackPlanner(RulePlanner(), SarvamIntentClassifier(fake, ["m"]))
    assert [t.intent for t in planner.plan("what is my emi")] == [Intent.EMI_DUE_QUERY] and fake.calls == 0


def test_classifier_failure_keeps_unknown():
    planner = FallbackPlanner(RulePlanner(), SarvamIntentClassifier(FakeSarvam(fail=True), ["m", "n"]))
    assert [t.intent for t in planner.plan("blah blah")] == [Intent.UNKNOWN]


# ---------------- end to end (verified borrower)

def test_collector_name_is_declined_without_a_ticket(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    r = say("what is the collctr nme")
    assert r["replies"][0]["template_id"] == "restricted_info" and not r["tickets"]
    assert rt.store.accounts[a["account_id"]]["collector_name"] not in r["replies"][0]["text"]


def test_typo_interest_rate_gets_the_loan_summary(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    r = say("what is the intrst rate")
    assert r["replies"][0]["template_id"] == "loan_rate" and no_guard_blocks(r)


def test_account_question_without_llm_uses_the_facts_template(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    r = say("when does my loan end")
    assert r["replies"][0]["template_id"] == "account_facts" and no_guard_blocks(r)


def test_account_question_llm_answer_is_grounded(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    acct = rt.store.accounts[a["account_id"]]
    rt.answerer = SarvamAnswerer(FakeSarvam(f"Your loan amount was ₹{float(acct['sanction_amount']):,.2f}."), ["m"])
    r = say("what was my loan amount")
    assert r["replies"][0]["template_id"] == "account_answer_llm" and no_guard_blocks(r)


def test_account_question_llm_invented_number_falls_back(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    rt.answerer = SarvamAnswerer(FakeSarvam("Your loan amount was ₹9,99,999.00."), ["m"])
    r = say("what was my loan amount")
    assert r["replies"][0]["template_id"] == "account_facts"


def test_off_topic_message_is_answered_not_ticketed(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    rt.planner = FallbackPlanner(RulePlanner(), SarvamIntentClassifier(FakeSarvam("out_of_scope"), ["m"]))
    r = say("will it rain tomorrow")
    assert r["replies"][0]["template_id"] == "out_of_scope" and not r["tickets"]


# ---------------- WhatsApp round 2: focused answers, translated voice questions

@pytest.mark.parametrize("text", ["I will tell you how many EMIs I have paid now.",   # Bengali voice -> English
                                  "How much EMI have I paid till today?", "How many EMIs have I paid so far?"])
def test_question_about_paid_emis_is_not_a_claim(text):
    assert [t.intent for t in RulePlanner().plan(text)] == [Intent.ACCOUNT_QUESTION]


def test_claim_with_proof_is_still_a_claim():
    intents = [t.intent for t in RulePlanner().plan("already paid UTR 512398765432, how many emis are left")]
    assert Intent.PAYMENT_CLAIM in intents


def test_interest_rate_question_gets_only_the_rate(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    r = say("what is my interst rate")
    text = r["replies"][0]["text"]
    assert r["replies"][0]["template_id"] == "loan_rate" and "interest rate" in text
    assert "outstanding" not in text and "Overdue" not in text and no_guard_blocks(r)


def test_loan_details_still_gets_the_full_summary(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    r = say("show my loan details")
    assert r["replies"][0]["template_id"] == "loan_summary" and no_guard_blocks(r)


def test_voice_question_about_paid_emis_gets_one_answer(settings, borrower):
    from servicing_agent.contracts.state import InboundMessage, MediaEvidence
    rt, a, say = client_for(settings, borrower)
    msg = InboundMessage(message_id="v1", sender=borrower[1], type="voice", media_id="V1",
                         media=MediaEvidence(kind="voice_note", transcript="I will tell you how many EMIs I have paid now.",
                                             language="bn", provider="fake"))
    res = rt.run_turn(msg)
    assert [x.template_id for x in res.replies] == ["account_facts"]          # no "share your UTR" after it
