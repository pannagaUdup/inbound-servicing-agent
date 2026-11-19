"""Payment history, loan summary and charges for the verified account."""

import pytest
from fastapi.testclient import TestClient

from servicing_agent.agent.planner import RulePlanner
from servicing_agent.contracts.state import Intent


@pytest.mark.parametrize("text,expected", [
    ("show my payment history", [Intent.PAYMENT_HISTORY]),
    ("what is my outstanding amount", [Intent.LOAN_SUMMARY]),      # the inbound menu's "Account status"
    ("what is my interest rate", [Intent.LOAN_SUMMARY]),
    ("how many emis are left", [Intent.LOAN_SUMMARY]),
    ("loan status", [Intent.LOAN_SUMMARY]),
    ("why was I charged bounce charges", [Intent.CHARGES_QUERY]),
    ("why extra charge on my loan", [Intent.CHARGE_REVERSAL]),     # disputes still go to a person
    ("please remove the late charge", [Intent.WAIVER_REQUEST]),
    ("I paid, also show my last 3 payments", [Intent.PAYMENT_CLAIM, Intent.PAYMENT_HISTORY]),
    ("what is my emi", [Intent.EMI_DUE_QUERY]),
])
def test_planner_routes_account_questions(text, expected):
    assert [t.intent for t in RulePlanner().plan(text)] == expected


def client_for(settings, borrower, verify=True):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(settings)
    c = TestClient(app)
    if verify:
        for t in ("hi", a["account_id"], "123456"):
            c.post("/v1/simulate/messages", json={"sender": sender, "text": t})
    say = lambda text: c.post("/v1/simulate/messages", json={"sender": sender, "text": text}).json()
    return app.state.runtime, a, say


def no_guard_blocks(r):
    return not any(e["type"] == "output_blocked" for e in r["events"])


def test_payment_history_lists_recent_payments_with_masked_utrs(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    r = say("show my payment history")
    text = r["replies"][0]["text"]
    assert r["replies"][0]["template_id"] in ("payment_history", "payment_history_empty") and no_guard_blocks(r)
    for p in rt.store.payments_by_account[a["account_id"]]:
        assert p["utr"] not in text                                   # full UTRs never shown
    if r["replies"][0]["template_id"] == "payment_history":
        assert "UTR ••••" in text and text.count("\n•") <= 5


def test_loan_summary_and_charges_come_from_the_lms(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    acc = rt.store.accounts[a["account_id"]]
    r = say("what is my loan summary")
    assert r["replies"][0]["template_id"] == "loan_summary" and no_guard_blocks(r)
    assert f"{acc['interest_rate']:.2f}%" in r["replies"][0]["text"]
    assert f"{acc['principal_outstanding']:,.2f}" in r["replies"][0]["text"]

    r = say("why was I charged bounce charges")
    assert r["replies"][0]["template_id"] in ("charges", "charges_none") and no_guard_blocks(r)
    if acc["bounce_charges"] or acc["penal_charges"]:
        assert f"{acc['bounce_charges'] + acc['penal_charges']:,.2f}" in r["replies"][0]["text"]
    assert "waive" not in r["replies"][0]["text"].lower()          # explains, never promises


def test_internal_fields_never_leave_the_gateway(settings, borrower):
    rt, a, say = client_for(settings, borrower)
    acc = rt.store.accounts[a["account_id"]]
    text = " ".join(say(q)["replies"][0]["text"] for q in ("loan summary", "show my payment history", "what are these charges"))
    assert str(acc["bureau_score"]) not in text and acc["collector_name"] not in text


def test_account_details_need_verification(settings, borrower):
    _, _, say = client_for(settings, borrower, verify=False)
    for q in ("show my payment history", "what is my interest rate", "what are these charges"):
        r = say(q)
        assert r["replies"][0]["template_id"] == "ask_account" and not r["verified"]
