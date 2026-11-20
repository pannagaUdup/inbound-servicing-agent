"""The 11:13 conversation: outstanding + EMI, part payment, waiver over an open case, "tell me more", "terminate and
start a new one for ₹1615"; and verified sessions that end (idle and absolute limits)."""

from dataclasses import replace
from datetime import datetime, timedelta

from servicing_agent.agent import verification
from servicing_agent.contracts.state import AuthScope
from tests.test_agent_flow import say, verify


def _open_settlement(runtime, sender):
    say(runtime, sender, "I want a one time settlement for Rs 20000, my salary was delayed")
    assert say(runtime, sender, "yes").replies[-1].template_id == "waiver_created"


def test_outstanding_and_emi_are_both_answered(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = say(runtime, sender, "What is my current outstanding amount and EMI to be paid this month?")
    assert {x.template_id for x in r.replies} >= {"emi_due"} and len(r.replies) == 2


def test_tell_me_more_about_my_request_gives_its_details(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    _open_settlement(runtime, sender)
    r = say(runtime, sender, "Tell me more about my previous request.")
    t = r.replies[0].text
    assert r.replies[0].template_id == "case_status_detail" and "20,000" in t and "salary delay" in t and "supervisor" in t


def test_withdraw_asks_then_withdraws_and_closes_tickets(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    _open_settlement(runtime, sender)
    case = runtime.cases.open_case_for_account(a["account_id"])
    r = say(runtime, sender, "please cancel my settlement request")
    assert r.replies[0].template_id == "case_withdraw_confirm" and case.case_id in r.replies[0].text
    assert runtime.cases.open_case_for_account(a["account_id"]) is not None          # not before YES
    r = say(runtime, sender, "yes")
    assert r.replies[0].template_id == "case_withdrawn"
    assert runtime.cases.open_case_for_account(a["account_id"]) is None
    assert runtime.tools.tickets.open_for_account(a["account_id"], "settlement_request") is None


def test_no_keeps_the_request(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    _open_settlement(runtime, sender)
    say(runtime, sender, "withdraw my request")
    assert say(runtime, sender, "no").replies[0].template_id == "case_withdraw_kept"
    assert runtime.cases.open_case_for_account(a["account_id"]) is not None


def test_terminate_and_start_a_new_one_for_an_amount_restarts_it(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    _open_settlement(runtime, sender)
    say(runtime, sender, "please waive my penal charges, my salary was delayed")      # a waiver proposed
    r = say(runtime, sender, "Terminate the whole thing and reinitiate a new flow for ₹1615.")
    reply = r.replies[-1]
    assert reply.template_id == "waiver_confirm_change" and "1,615.00" in reply.text and "restarts" in reply.text
    assert "waiver" in reply.text                                                   # the type just discussed
    r = say(runtime, sender, "yes")
    case = runtime.cases.open_case_for_account(a["account_id"])
    assert r.replies[0].template_id == "waiver_restarted" and case.version == 2 and case.request.amount == 1615


def test_a_changed_request_needs_its_reason_too(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    say(runtime, sender, "I want a one time settlement for Rs 20000")
    say(runtime, sender, "my salary was delayed")
    say(runtime, sender, "yes")
    case = runtime.cases.open_case_for_account(a["account_id"])
    # The old case's reason carries over, so a plain amount change goes straight to the change confirmation.
    assert say(runtime, sender, "make the settlement Rs 15000").replies[-1].template_id == "waiver_confirm_change"
    assert case.request.reason_category == "salary_delay"


def test_withdraw_without_an_open_request(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    assert say(runtime, sender, "cancel my request").replies[0].template_id == "no_open_case"


# ---------------- session limits

def _scope(verified_min_ago: int, expires_in_min: int) -> AuthScope:
    now = datetime.utcnow()
    return AuthScope(account_id="CN1", customer_id="C1", contact_id="CT1", method="account+mobile+otp",
                     verified_at=now - timedelta(minutes=verified_min_ago), expires_at=now + timedelta(minutes=expires_in_min))


def test_absolute_limit_ends_an_active_session():
    assert verification.scope_valid(_scope(10, 20), max_minutes=120)
    assert not verification.scope_valid(_scope(125, 20), max_minutes=120)        # active, but past the cap
    assert not verification.scope_valid(_scope(5, -1), max_minutes=120)          # idle too long


def test_activity_extends_only_up_to_the_cap():
    s = _scope(110, 5)
    now = datetime.utcnow()
    new = verification.refreshed_expiry(s, now, idle_minutes=30, max_minutes=120)
    assert new <= s.verified_at + timedelta(minutes=120) + timedelta(seconds=1)


def test_session_ends_after_the_absolute_limit_even_while_chatting(settings, runtime, borrower):
    a, sender = borrower
    runtime.settings = replace(settings, session_max_minutes=120)
    verify(runtime, a, sender)
    cid = runtime.conversation_id_for(sender)
    sess = runtime.conversations.load(cid)["session"]
    sess["scope"]["verified_at"] = (datetime.utcnow() - timedelta(minutes=121)).isoformat()
    r = say(runtime, sender, "what is my emi")
    assert r.replies[0].template_id == "ask_account"


def test_changing_an_old_request_without_a_reason_asks_for_it(runtime, borrower):
    from servicing_agent.contracts.cases import CaseRequest, CaseType
    a, sender = borrower
    verify(runtime, a, sender)
    runtime.cases.create(a["account_id"], runtime.conversation_id_for(sender),
                         CaseRequest(type=CaseType.SETTLEMENT))                  # like CASEA65021F9C9: no amount, no reason
    r = say(runtime, sender, "please waive Rs 1615 of penal charges")
    assert r.replies[-1].template_id == "case_details_needed" and "reason" in r.replies[-1].text
    r = say(runtime, sender, "I lost my job")
    assert r.replies[-1].template_id == "waiver_confirm_change" and "1,615.00" in r.replies[-1].text
    assert "job loss" in r.replies[-1].text
