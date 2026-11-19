"""End-to-end turns through the LangGraph agent and the FastAPI app."""

from fastapi.testclient import TestClient

from servicing_agent.agent.graph import new_message_id
from servicing_agent.contracts.state import InboundMessage, MediaEvidence


def say(runtime, sender, text=None, **kw):
    return runtime.run_turn(InboundMessage(message_id=new_message_id(), sender=sender, text=text, **kw))


def verify(runtime, account, sender):
    say(runtime, sender, account["account_id"])
    return say(runtime, sender, "123456")


def test_no_account_data_before_verification(runtime, borrower):
    a, sender = borrower
    r = say(runtime, sender, "what is my foreclosure amount")
    assert not r.verified and r.replies[0].template_id == "ask_account"
    r = say(runtime, sender, a["account_id"])
    assert r.replies[0].template_id == "otp_sent_neutral"
    r = say(runtime, sender, "123456")
    assert r.verified and r.replies[0].template_id == "foreclosure_quote" and r.resolution == "auto"


def test_wrong_account_gets_identical_reply_and_never_verifies(runtime, borrower):
    a, sender = borrower
    other = next(x for x in runtime.store.accounts if x != a["account_id"])
    say(runtime, sender, "emi due?")
    r1 = say(runtime, sender, other)
    assert r1.replies[0].template_id == "otp_sent_neutral"   # same reply as a match
    r2 = say(runtime, sender, "123456")
    assert not r2.verified


def test_unregistered_number_cannot_verify(runtime, borrower):
    a, _ = borrower
    stranger = "919000000001"
    say(runtime, stranger, "statement please")
    say(runtime, stranger, a["account_id"])
    assert not say(runtime, stranger, "123456").verified


def test_otp_lockout_after_three_failures(runtime, borrower):
    a, sender = borrower
    say(runtime, sender, "emi")
    say(runtime, sender, a["account_id"])
    for _ in range(3):
        r = say(runtime, sender, "000000")
    assert r.replies[0].template_id == "verify_failed"
    assert say(runtime, sender, "111111").replies[0].template_id == "locked"


def test_other_account_number_triggers_reverification(runtime, borrower):
    a, sender = borrower
    say(runtime, sender, "emi due?")
    verify(runtime, a, sender)
    other = next(x for x in runtime.store.accounts if x != a["account_id"])
    r = say(runtime, sender, f"foreclosure amount for {other}")
    assert not r.verified and r.replies[0].template_id == "otp_sent_neutral"


def test_fake_screenshot_is_not_accepted_and_is_flagged(runtime, borrower):
    a, sender = borrower
    p = next(p for p in runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    say(runtime, sender, "already paid")
    verify(runtime, a, sender)
    r = say(runtime, sender, type="image",
            media=MediaEvidence(kind="upi_receipt", utr=p["utr"], amount=p["amount"] * 3, txn_date=p["txn_date"]))
    assert r.replies[0].template_id == "claim_unverified"                 # neutral to the borrower
    assert r.tickets and r.tickets[0]["queue"] == "fraud_review"          # escalated silently
    r = say(runtime, sender, type="image",
            media=MediaEvidence(kind="upi_receipt", utr=p["utr"], amount=p["amount"], txn_date=p["txn_date"]))
    assert r.replies[0].template_id == "claim_matched"


def test_waiver_change_requires_confirmation_then_restarts(runtime, borrower):
    a, sender = borrower
    say(runtime, sender, "please waive my penal charges of Rs 5000")
    r = verify(runtime, a, sender)
    assert r.replies[0].template_id == "case_confirm" and runtime.cases.open_case_for_account(a["account_id"]) is None
    r = say(runtime, sender, "yes")
    assert r.replies[0].template_id == "waiver_created"
    r = say(runtime, sender, "any update? please approve fast")
    assert r.replies[0].template_id == "case_status"
    r = say(runtime, sender, "can you make the waiver Rs 3000")
    assert r.replies[0].template_id == "waiver_confirm_change"
    r = say(runtime, sender, "yes")
    assert r.replies[0].template_id == "waiver_restarted"
    case = runtime.cases.open_case_for_account(a["account_id"])
    assert case.version == 2 and case.request.amount == 3000


def test_distress_goes_straight_to_a_human(runtime, borrower):
    _, sender = borrower
    r = say(runtime, sender, "I want to end my life")
    assert r.replies[0].template_id == "handoff" and r.tickets[0]["queue"] == "priority_care"


def test_api_simulate_and_context_pack(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    client = TestClient(create_app(settings))
    a, sender = borrower
    post = lambda text: client.post("/v1/simulate/messages", json={"sender": sender, "text": text}).json()
    post("I want a one time settlement for Rs 20000, I lost my job")
    post(a["account_id"])
    r = post("123456")
    # "I lost my job" was said before verification; the triage survives the OTP step.
    assert r["verified"] and [x["template_id"] for x in r["replies"]] == ["hardship_ack", "case_confirm"]
    r = post("yes")
    assert [x["template_id"] for x in r["replies"]] == ["waiver_created"]
    tri = r["tickets"][0]["summary"]["triage"]
    assert tri["hardship"] and tri["priority"] == "high" and tri["needs_approval"]
    case_id = r["tickets"][0]["summary"]["case_id"]
    pack = client.get(f"/v1/cases/{case_id}/versions/1/context").json()
    from servicing_agent.platform.context_pack import REQUIRED_SECTIONS
    assert all(k in pack for k in REQUIRED_SECTIONS)
    assert pack["loan_position"]["principal_outstanding"] == a["principal_outstanding"]
    case = client.get(f"/v1/cases/{case_id}").json()
    etag = case["versions"][0]["etag"]
    ok = client.post(f"/v1/cases/{case_id}/versions/1/decisions",
                     json={"role": "approver", "decision": "approve", "rationale": "policy band ok",
                           "reviewer_id": "r1", "etag": etag})
    assert ok.status_code == 200
    stale = client.post(f"/v1/cases/{case_id}/versions/1/decisions",
                        json={"role": "approver", "decision": "approve", "rationale": "again",
                              "reviewer_id": "r1", "etag": etag})
    assert stale.status_code == 409
