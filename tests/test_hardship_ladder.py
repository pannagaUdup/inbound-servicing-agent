"""Waivers and settlements as a last resort (policy HRD-03, WVR-03/04/05, STL-03..07)."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from servicing_agent.agent.handlers import hardship_stance, is_refusal
from servicing_agent.agent.planner import FallbackPlanner, RulePlanner
from servicing_agent.contracts.state import Intent
from tests.test_agent_flow import say, verify


@pytest.fixture
def ladder(settings):
    from servicing_agent.platform.runtime import build_runtime
    return build_runtime(replace(settings, hardship_ladder=True))


def _shape(runtime, borrower, monkeypatch, band: str, paid_share: float, preapproved: str = "N",
           principal: float = 50000.0, sanction: float = 100000.0, bounce: float = 1200.0, penal: float = 300.0):
    """The borrower's loan: DPD band, overdue ₹10,000, confirmed payments in the last 30 days, pre-approval flag."""
    a, sender = borrower
    acc = runtime.store.accounts[a["account_id"]]
    dpd = int(band.split("-")[0]) + 5
    for k, v in {"dpd_band": band, "dpd": dpd, "overdue_amount": 10000.0, "preapproved_settlement": preapproved,
                 "principal_outstanding": principal, "sanction_amount": sanction, "bounce_charges": bounce,
                 "penal_charges": penal}.items():
        monkeypatch.setitem(acc, k, v)
    # Only this test's payments count in the last 30 days (other tests add payments to the shared, cached store).
    cutoff = (date.today() - timedelta(days=31)).isoformat()
    pays = [p for p in runtime.store.payments_by_account[a["account_id"]] if p["txn_date"] < cutoff]
    if paid_share:
        pays.append({"txn_id": "TXNLADDER", "utr": "599999999999", "account_id": a["account_id"],
                     "amount": round(10000.0 * paid_share, 2), "channel": "UPI", "status": "success",
                     "txn_date": (date.today() - timedelta(days=3)).isoformat(), "settled_date": "",
                     "payee_vpa": "collect@cnbank"})
    monkeypatch.setitem(runtime.store.payments_by_account, a["account_id"], pays)
    verify(runtime, a, sender)
    return a, sender


def _t(r):
    return [x.template_id for x in r.replies]


def _rounds(runtime, sender, n=3):
    msgs = ["I can't pay this month, I lost my job", "no I can't", "not possible"]
    return [_t(say(runtime, sender, m))[-1] for m in msgs[:n]]


def test_stances():
    assert hardship_stance("I am in no state of paying at all") == "refuse_all"
    assert hardship_stance("can I pay the settlement in 3 instalments") == "ask_instalments"
    assert hardship_stance("please settle it") == "ask_settlement"
    assert hardship_stance("waive the rest please") == "ask_waiver"
    assert hardship_stance("I can pay 6000", amount=6000) == "pay_part"
    assert hardship_stance("I can't pay this month") == "unable_now"
    assert is_refusal("no I can't") and is_refusal("not possible") and not is_refusal("no, what is my emi?")


def test_model_extra_labels_on_cant_pay_are_dropped():
    class Clf:
        def classify(self, text):
            return [Intent.PAYMENT_DIFFICULTY, Intent.CHARGE_REVERSAL, Intent.ESCALATION_REQUEST]
    out = FallbackPlanner(RulePlanner(), Clf()).plan("I won't pay full amount, what will you do?")
    assert [t.intent for t in out] == [Intent.PAYMENT_DIFFICULTY]                 # no charges ticket, no escalation


def test_part_payment_gets_a_link_and_an_early_ask_is_not_raised(ladder, borrower, monkeypatch):
    a, sender = _shape(ladder, borrower, monkeypatch, "320-330", 0)
    r = say(ladder, sender, "I won't be able to pay the full amount, I can pay ₹6000")
    assert _t(r) == ["hardship_partial"] and "WAIVER" not in r.replies[0].text
    r = say(ladder, sender, "I want a one time settlement")
    assert _t(r) == ["hardship_not_yet", "hardship_encourage_1"] and not r.tickets


def test_the_12_15_chat_no_longer_jumps_to_settlement(ladder, borrower, monkeypatch):
    """3 rounds, more than 10 days past due, not pre-approved: collections team, with the reason why."""
    a, sender = _shape(ladder, borrower, monkeypatch, "320-330", 0)
    assert _rounds(ladder, sender) == ["hardship_encourage_1", "hardship_encourage_2", "hardship_encourage_3"]
    r = say(ladder, sender, "My boss kicked me out, I won't be paying my amount")
    assert _t(r) == ["hardship_collections_why"] and r.tickets[0]["queue"] == "collections"
    text = r.replies[0].text
    assert "325 days past due (a waiver needs between 10 and 30 days past due)" in text and "70%" not in text
    assert r.tickets[0]["ticket_id"] in text and "settle" not in text.lower()
    again = say(ladder, sender, "I really can't pay")
    assert _t(again) == ["hardship_collections_why"] and not again.tickets             # same ticket


def test_no_drops_the_waiver_and_no_settlement_follows(ladder, borrower, monkeypatch):
    a, sender = _shape(ladder, borrower, monkeypatch, "10-20", 0, preapproved="Y", principal=20000.0)
    _rounds(ladder, sender)
    assert _t(say(ladder, sender, "I won't be able to pay"))[-1] == "hardship_offer_cond_waiver"
    r = say(ladder, sender, "no")
    assert _t(r) == ["waiver_declined_ack"] and not r.tickets and ladder.cases.open_case_for_account(a["account_id"]) is None
    later = [_t(say(ladder, sender, m))[-1] for m in ("I still can't pay", "I won't be able to pay anything")]
    assert all(t.startswith("hardship_encourage") for t in later)                 # no waiver, no settlement

def test_0_10_band_is_not_eligible_for_a_waiver(ladder, borrower, monkeypatch):
    a, sender = _shape(ladder, borrower, monkeypatch, "0-10", 0.9)
    _rounds(ladder, sender)
    r = say(ladder, sender, "I won't be able to pay")
    assert _t(r) == ["hardship_collections_why"] and "between 10 and 30 days past due" in r.replies[0].text


def test_a_loan_without_charges_has_nothing_to_waive(ladder, borrower, monkeypatch):
    a, sender = _shape(ladder, borrower, monkeypatch, "20-30", 0, bounce=0, penal=0)
    _rounds(ladder, sender)
    r = say(ladder, sender, "I won't be able to pay")
    assert _t(r) == ["hardship_collections_why"] and "no late-payment or bounce charges" in r.replies[0].text


def test_explicit_waiver_ask_gets_the_conditional_offer_when_eligible(ladder, borrower, monkeypatch):
    a, sender = _shape(ladder, borrower, monkeypatch, "20-30", 0)
    _rounds(ladder, sender)
    r = say(ladder, sender, "please waive my charges")
    assert _t(r) == ["hardship_offer_cond_waiver"] and ladder.cases.open_case_for_account(a["account_id"]) is None


def test_not_waiver_eligible_but_preapproved_gets_the_one_time_link(ladder, borrower, monkeypatch):
    a, sender = _shape(ladder, borrower, monkeypatch, "210-220", 0, preapproved="Y", principal=1000.0)
    _rounds(ladder, sender)
    r = say(ladder, sender, "I can't pay at all")
    assert _t(r) == ["hardship_offer_preapproved"] and "6,600.00" in r.replies[0].text       # 60% of 11,000


def test_instalments_for_the_settlement_go_to_collections(ladder, borrower, monkeypatch):
    a, sender = _shape(ladder, borrower, monkeypatch, "210-220", 0, preapproved="Y", principal=1000.0)
    _rounds(ladder, sender)
    say(ladder, sender, "I can't pay at all")
    r = say(ladder, sender, "can I pay it in 3 instalments?")
    assert _t(r) == ["hardship_collections"] and r.tickets[0]["request_type"] == "collections_review"
    assert "instalments" in r.tickets[0]["summary"]["why"]


def test_high_recovery_in_the_bucket_is_preapproved_by_policy(ladder, borrower, monkeypatch):
    a, sender = _shape(ladder, borrower, monkeypatch, "0-10", 0, preapproved="N", principal=5000.0, sanction=100000.0,
                       bounce=0, penal=0)                                     # nothing to waive; 95% recovered
    _rounds(ladder, sender)
    assert _t(say(ladder, sender, "I won't be able to pay"))[-1] == "hardship_offer_preapproved"


def test_tell_me_after_encouragement_explains_paying_not_waivers(ladder, borrower, monkeypatch):
    a, sender = _shape(ladder, borrower, monkeypatch, "320-330", 0)
    say(ladder, sender, "I won't be able to pay this month")
    r = say(ladder, sender, "tell me")
    assert _t(r) == ["policy_explain"] and "WAIVER" not in r.replies[0].text and "SETTLE" not in r.replies[0].text


def test_a_long_no_about_something_else_is_not_the_answer_to_an_old_question(ladder, borrower, monkeypatch):
    from servicing_agent.contracts.cases import CaseRequest, CaseType
    a, sender = _shape(ladder, borrower, monkeypatch, "320-330", 0)
    ladder.cases.create(a["account_id"], ladder.conversation_id_for(sender),
                        CaseRequest(type=CaseType.SETTLEMENT, amount=1000, reason_category="job_loss"))
    assert _t(say(ladder, sender, "please waive Rs 2615 of penal charges, my salary was delayed"))[-1] == "waiver_confirm_change"
    say(ladder, sender, "I won't be able to pay the remaining amount on time")
    r = say(ladder, sender, "No, I am in a very difficult situation. I can't pay any amount right now. I'm very sorry.")
    assert _t(r)[-1].startswith("hardship_") and "waiver_change_kept" not in _t(r)
    assert ladder.cases.open_case_for_account(a["account_id"]).version == 1


def test_a_short_no_still_answers_the_question(ladder, borrower, monkeypatch):
    from servicing_agent.contracts.cases import CaseRequest, CaseType
    a, sender = _shape(ladder, borrower, monkeypatch, "320-330", 0)
    ladder.cases.create(a["account_id"], ladder.conversation_id_for(sender),
                        CaseRequest(type=CaseType.SETTLEMENT, amount=1000, reason_category="job_loss"))
    say(ladder, sender, "please waive Rs 2615 of penal charges, my salary was delayed")
    assert _t(say(ladder, sender, "no, keep it"))[-1] == "waiver_change_kept"


def test_an_unanswered_question_expires(ladder, borrower, monkeypatch):
    from datetime import datetime
    from servicing_agent.contracts.cases import CaseRequest, CaseType
    a, sender = _shape(ladder, borrower, monkeypatch, "320-330", 0)
    ladder.cases.create(a["account_id"], ladder.conversation_id_for(sender),
                        CaseRequest(type=CaseType.SETTLEMENT, amount=1000, reason_category="job_loss"))
    say(ladder, sender, "please waive Rs 2615 of penal charges, my salary was delayed")
    sess = ladder.conversations.load(ladder.conversation_id_for(sender))["session"]
    sess["pending_change"]["asked_at"] = (datetime.utcnow() - timedelta(minutes=60)).isoformat()
    assert _t(say(ladder, sender, "no"))[-1] != "waiver_change_kept"






def _approve(client, case):
    for role in ("approver", "compliance"):
        c = client.get(f"/v1/cases/{case.case_id}").json()
        v = c["versions"][-1]
        r = client.post(f"/admin/api/cases/{case.case_id}/versions/{v['version']}/decisions",
                        json={"role": role, "decision": "approve", "rationale": "ok", "reviewer_id": "t",
                              "etag": v["etag"]})
        assert r.status_code == 200, r.text


def _waiver_app(settings, borrower, monkeypatch):
    from fastapi.testclient import TestClient
    from servicing_agent.platform.api.app import create_app
    app = create_app(replace(settings, hardship_ladder=True, public_base_url="https://demo.example"))
    client, rt = TestClient(app), app.state.runtime
    a, sender = _shape(rt, borrower, monkeypatch, "10-20", 0)
    _rounds(rt, sender)
    offer = say(rt, sender, "I won't be able to pay the full amount").replies[0]
    assert offer.template_id == "hardship_offer_cond_waiver" and "same day" in offer.text
    assert "1,500.00" in offer.text and "8,500.00" in offer.text and "YES" in offer.text     # 10,000 - 1,500 charges
    r = say(rt, sender, "yes")
    assert _t(r) == ["cond_waiver_requested"]                                               # approval first
    case = rt.cases.open_case_for_account(a["account_id"])
    assert case.request.amount == 1500.0 and case.request.plan == "pay_same_day" and case.case_id in r.replies[0].text
    assert not rt.paylinks.list_pending()                                                   # no link before approval
    return client, rt, a, sender, case


def test_waiver_approved_then_paid_same_day_clears_the_overdue(settings, borrower, monkeypatch):
    client, rt, a, sender, case = _waiver_app(settings, borrower, monkeypatch)
    n = len(rt.channel.sent)
    _approve(client, case)
    sent = [m["payload"].get("text", {}).get("body", "") for m in rt.channel.sent[n:]]
    approved = next(b for b in sent if "has been approved" in b)
    assert "₹8,500.00" in approved and "11:59 PM" in approved and "withdrawn" in approved
    link = rt.paylinks.list_pending()[0]
    assert link["amount"] == 8500.0 and rt.paylinks.get(link["link_id"])["expires_at"]
    client.post(f"/pay/{link['link_id']}/confirm")
    acc = rt.store.accounts[a["account_id"]]
    assert acc["overdue_amount"] == 0 and acc["bounce_charges"] == 0 and acc["penal_charges"] == 0 and acc["dpd"] == 0
    sent = [m["payload"].get("text", {}).get("body", "") for m in rt.channel.sent[n:]]
    assert any("charges of ₹1,500.00 are waived" in b for b in sent)


def test_waiver_not_paid_by_the_deadline_is_withdrawn(settings, borrower, monkeypatch):
    from datetime import datetime, timedelta
    from servicing_agent.platform.db import PayLink
    client, rt, a, sender, case = _waiver_app(settings, borrower, monkeypatch)
    _approve(client, case)
    link_id = rt.paylinks.list_pending()[0]["link_id"]
    with rt.session_factory.begin() as s:
        s.get(PayLink, link_id).expires_at = datetime.utcnow() - timedelta(minutes=1)    # midnight passed
    n = len(rt.channel.sent)
    assert rt.paylinks.expire_due() == 1
    assert rt.paylinks.get(link_id)["status"] == "expired"
    assert any("waiver has been withdrawn" in m["payload"].get("text", {}).get("body", "") for m in rt.channel.sent[n:])
    assert rt.store.accounts[a["account_id"]]["overdue_amount"] == 10000.0                  # charges still owed
    assert rt.paylinks.pay(link_id)["status"] == "expired"                                  # can't pay after


def test_a_stale_band_does_not_block_the_waiver(ladder, borrower, monkeypatch):
    """The 20 Nov chat: 13 days past due but a stored band of 0-10. The band now follows the DPD."""
    a, sender = _shape(ladder, borrower, monkeypatch, "10-20", 0)
    monkeypatch.setitem(ladder.store.accounts[a["account_id"]], "dpd_band", "0-10")
    _rounds(ladder, sender)
    r = say(ladder, sender, "I won't be able to pay")
    assert _t(r) == ["hardship_offer_cond_waiver"] and "same day" in r.replies[0].text


def test_nothing_overdue_is_not_called_past_due(ladder, borrower, monkeypatch):
    """Overdue cleared but a leftover dpd of 13: the reason is 'nothing overdue', not '13 days past due'."""
    a, sender = _shape(ladder, borrower, monkeypatch, "10-20", 0)
    monkeypatch.setitem(ladder.store.accounts[a["account_id"]], "overdue_amount", 0.0)
    _rounds(ladder, sender)
    r = say(ladder, sender, "I won't be able to pay")
    text = r.replies[-1].text
    assert "nothing is overdue" in text and "days past due" not in text


def test_a_change_of_dpd_band_starts_the_rounds_again(ladder, borrower, monkeypatch):
    """14:11 rounds on a 0-10 loan, then the loan became 10-20: the first 'can't pay' is round 1 again."""
    a, sender = _shape(ladder, borrower, monkeypatch, "0-10", 0)
    _rounds(ladder, sender)
    monkeypatch.setitem(ladder.store.accounts[a["account_id"]], "dpd", 13)              # the band follows dpd
    assert _t(say(ladder, sender, "I won't be able to make the full payment right now"))[-1] == "hardship_encourage_1"


def test_starting_over_with_hi_resets_the_rounds(settings, borrower, monkeypatch):
    from servicing_agent.agent.handlers import reset_ladder
    from servicing_agent.platform.runtime import build_runtime
    rt = build_runtime(replace(settings, hardship_ladder=True))
    a, sender = _shape(rt, borrower, monkeypatch, "10-20", 0)
    _rounds(rt, sender)
    reset_ladder(rt.session_context, a["account_id"])                     # what 'hi' does on WhatsApp
    assert _t(say(rt, sender, "I won't be able to pay"))[-1] == "hardship_encourage_1"


def test_happy_flow_as_described(settings, borrower, monkeypatch):
    """New session, 10-30 DPD: 3 rounds of encouragement (any 'can't' reply counts), then the waiver offer;
    YES -> approval; approved -> same-day link; paid -> overdue 0."""
    from fastapi.testclient import TestClient
    from servicing_agent.platform.api.app import create_app
    app = create_app(replace(settings, hardship_ladder=True, public_base_url="https://demo.example"))
    client, rt = TestClient(app), app.state.runtime
    a, sender = _shape(rt, borrower, monkeypatch, "10-20", 0)
    msgs = ["I'm unable to pay right now", "I lost my job, I can't", "no, not possible", "I really can't pay this month"]
    assert [_t(say(rt, sender, m))[-1] for m in msgs] == [
        "hardship_encourage_1", "hardship_encourage_2", "hardship_encourage_3", "hardship_offer_cond_waiver"]
    assert _t(say(rt, sender, "yes")) == ["cond_waiver_requested"]
    case = rt.cases.open_case_for_account(a["account_id"])
    _approve(client, case)
    link = rt.paylinks.list_pending()[0]
    assert link["amount"] == 8500.0
    client.post(f"/pay/{link['link_id']}/confirm")
    assert rt.store.accounts[a["account_id"]]["overdue_amount"] == 0


def test_link_goes_out_on_supervisor_approval_and_compliance_reject_withdraws_it(settings, borrower, monkeypatch):
    client, rt, a, sender, case = _waiver_app(settings, borrower, monkeypatch)
    n = len(rt.channel.sent)
    c = client.get(f"/v1/cases/{case.case_id}").json(); v = c["versions"][-1]
    client.post(f"/admin/api/cases/{case.case_id}/versions/{v['version']}/decisions",
                json={"role": "approver", "decision": "approve", "rationale": "ok", "reviewer_id": "t", "etag": v["etag"]})
    links = rt.paylinks.list_pending()
    assert len(links) == 1 and links[0]["amount"] == 8500.0                          # sent before compliance
    assert any("has been approved" in m["payload"].get("text", {}).get("body", "") for m in rt.channel.sent[n:])
    c = client.get(f"/v1/cases/{case.case_id}").json(); v = c["versions"][-1]
    client.post(f"/admin/api/cases/{case.case_id}/versions/{v['version']}/decisions",
                json={"role": "compliance", "decision": "reject", "rationale": "no", "reviewer_id": "t", "etag": v["etag"]})
    assert rt.paylinks.get(links[0]["link_id"])["status"] == "cancelled"
    assert any("link sent earlier is withdrawn" in m["payload"].get("text", {}).get("body", "") for m in rt.channel.sent[n:])


def test_compliance_approval_after_the_link_sends_nothing_new(settings, borrower, monkeypatch):
    client, rt, a, sender, case = _waiver_app(settings, borrower, monkeypatch)
    _approve(client, case)                                                            # supervisor, then compliance
    from sqlalchemy import select
    from servicing_agent.platform.db import PayLink
    with rt.session_factory() as s:
        assert len(s.execute(select(PayLink).where(PayLink.purpose == "approved_waiver")).scalars().all()) == 1
