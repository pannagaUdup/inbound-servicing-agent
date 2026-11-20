"""Policy document retrieval, borrower wording from clauses, and context carried with escalations."""

from fastapi.testclient import TestClient

from servicing_agent.agent.policy import PolicyStore, clauses_for_case, consequences, policy

from tests.test_agent_flow import say, verify
from tests.test_claim_authenticity import _image, _post_payment
from tests.test_waiver_with_payment import _dues, _with_receipt


def test_policy_document_parses_into_clauses():
    p = policy()
    ids = [c.id for c in p.clauses]
    assert {"APR-01", "APR-02", "STL-01", "PPW-01", "CLM-02", "CLM-03", "ESC-01"} <= set(ids)
    assert p.get("STL-01").borrower and "credit" in p.get("STL-01").borrower
    assert p.version.startswith("2026")


def test_retrieval_picks_the_right_clauses():
    assert [c.id for c in consequences("waiver")] == ["APR-01", "APR-02"]
    assert [c.id for c in consequences("settlement")] == ["APR-01", "APR-02", "STL-01"]
    assert "PPW-01" in [c.id for c in clauses_for_case("waiver", partial=True)]
    assert "PPW-01" not in [c.id for c in clauses_for_case("waiver", partial=False)]
    assert policy().retrieve("will a settlement affect my credit score", k=1)[0].id == "STL-01"
    assert "CLM-03" in [c.id for c in policy().retrieve("receipt was edited fake tampered", k=2)]


def test_confirmation_wording_comes_from_the_document(runtime, borrower, tmp_path, monkeypatch):
    """Change the clause in the document -> the borrower sees the new wording, no code change."""
    import servicing_agent.agent.policy as pol
    doc = pol.POLICY_PATH.read_text().replace(
        "It isn't automatic: a supervisor reviews it first, then our compliance team checks it.",
        "Two people review it: first a supervisor, then compliance.")
    path = tmp_path / "policy.md"
    path.write_text(doc)
    monkeypatch.setattr(pol, "policy", lambda: PolicyStore.load(path))
    a, sender = borrower
    verify(runtime, a, sender)
    r = say(runtime, sender, "please waive my penal charges of Rs 1000, my salary was delayed")
    assert "Two people review it: first a supervisor, then compliance." in r.replies[0].text


def test_policy_question_is_answered_from_clauses(runtime, borrower):
    _, sender = borrower
    r = say(runtime, sender, "will a settlement affect my credit score")      # no verification needed
    assert r.replies[0].template_id == "policy_answer" and "settled" in r.replies[0].text
    assert "STL-01" in r.normalized["policy_clauses"]


def test_escalation_ticket_carries_the_context_bag(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    p = next(p for p in runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    r = _image(runtime, sender, utr=p["utr"], amount=p["amount"] + 3000, txn_date=p["txn_date"])
    bag = r.tickets[0]["summary"]["context_bag"]
    assert [c["id"] for c in bag["policy_clauses"]] == ["CLM-03", "CLM-04", "ESC-01"]
    assert bag["verified_account"] == a["account_id"] and bag["recent_conversation"]
    assert all(p["utr"] not in (m["text"] or "") for m in bag["recent_conversation"])   # masked
    assert any(e["type"] == "policy_applied" for e in r.events)


def test_failed_payment_escalation_carries_clm02(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = _image(runtime, sender, utr="998877665511", amount=1500.0, txn_date="2026-11-20")
    late = runtime.recheck_claim(r.conversation_id, r.followups[0]["slots"])
    ids = [c["id"] for c in late.tickets[0]["summary"]["context_bag"]["policy_clauses"]]
    assert "CLM-02" in ids and "ESC-01" in ids


def test_approval_context_pack_has_the_policy_and_the_bag(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    app = create_app(settings)
    rt, client = app.state.runtime, TestClient(app)
    a, sender = borrower
    post = lambda **kw: client.post("/v1/simulate/messages", json={"sender": sender, **kw}).json()
    for t in ("hi", a["account_id"], "123456"):
        post(text=t)
    due = _dues(rt, a["account_id"])
    _post_payment(rt, a["account_id"], "700000000010", round(due / 2, 2), "2026-11-19")
    post(text="I paid this, please waive the rest, my salary was delayed", type="image",
         receipt={"utr": "700000000010", "amount": round(due / 2, 2), "txn_date": "2026-11-19"})
    r = post(text="yes")
    case_id = r["tickets"][0]["summary"]["case_id"]
    pack = client.get(f"/v1/cases/{case_id}/versions/1/context").json()
    clause_ids = [c["id"] for c in pack["policy_clauses"]]
    assert {"APR-01", "APR-02", "APR-03", "PPW-01"} <= set(clause_ids)
    assert pack["context_bag"]["triage"]["needs_approval"] is True
    assert [t["queue"] for t in r["tickets"]] == ["waiver_approvals", "compliance_review"]
    assert r["tickets"][1]["summary"]["context_bag"]["policy_clauses"]
