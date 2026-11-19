"""Payment-claim audit ledger and idempotency (screenshot, voice and text claims)."""

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from servicing_agent.platform.db import PaymentClaim, Receipt


def setup(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(settings)
    rt = app.state.runtime
    client = TestClient(app)
    post = lambda **kw: client.post("/v1/simulate/messages", json={"sender": sender, **kw}).json()
    for t in ("hi", a["account_id"], "123456"):
        post(text=t)
    p = next(p for p in rt.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    return a, rt, post, p


def claims(rt):
    with rt.session_factory() as s:
        return s.scalars(select(PaymentClaim).order_by(PaymentClaim.id)).all()


def receipts(rt):
    with rt.session_factory() as s:
        return s.scalar(select(func.count()).select_from(Receipt))


def test_confirmed_screenshot_is_stored_and_resend_is_idempotent(settings, borrower):
    a, rt, post, p = setup(settings, borrower)
    rec = {"utr": p["utr"], "amount": p["amount"], "txn_date": p["txn_date"]}

    r1 = post(type="image", receipt=rec, message_id="wamid.A")
    assert r1["replies"][0]["template_id"] == "claim_matched"
    rows = claims(rt)
    assert len(rows) == 1
    c = rows[0]
    assert (c.utr, c.result, c.source, c.attempts) == (p["utr"], "MATCHED", "screenshot", 1)
    assert c.media_id == "wamid.A" and c.first_message_id == "wamid.A" and c.confirmed_at is not None
    assert c.matched_txn_id == p["txn_id"] and receipts(rt) == 1

    # Borrower sends the same screenshot again (a new message): confirmed again, not fraud.
    r2 = post(type="image", receipt=rec, message_id="wamid.B")
    assert r2["replies"][0]["template_id"] == "claim_already_confirmed"
    assert r2["tickets"] == []
    rows = claims(rt)
    assert len(rows) == 1 and rows[0].attempts == 2 and rows[0].last_message_id == "wamid.B"
    assert rows[0].result == "MATCHED" and receipts(rt) == 1           # still one receipt


def test_typed_utr_and_voice_claims_record_their_source(settings, borrower):
    a, rt, post, p = setup(settings, borrower)
    post(text=f"I paid, UTR {p['utr']}", message_id="wamid.T")
    assert claims(rt)[-1].source == "text"

    pays = [x for x in rt.store.payments_by_account[a["account_id"]] if x["status"] == "success"]
    if len(pays) > 1:
        q = pays[1]
        post(type="voice", transcript=f"I paid already, UTR {q['utr']}", message_id="wamid.V")
        c = claims(rt)[-1]
        assert c.source == "voice" and c.media_id == "wamid.V" and c.utr == q["utr"]


def test_resent_unmatched_claim_reuses_the_ticket(settings, borrower):
    a, rt, post, p = setup(settings, borrower)
    fake = {"utr": p["utr"], "amount": p["amount"] * 3 + 1, "txn_date": p["txn_date"]}     # edited amount
    r1 = post(type="image", receipt=fake, message_id="wamid.F1")
    assert r1["replies"][0]["template_id"] == "claim_unverified" and len(r1["tickets"]) == 1
    ticket_id = r1["tickets"][0]["ticket_id"]

    r2 = post(type="image", receipt=fake, message_id="wamid.F2")
    assert r2["replies"][0]["template_id"] == "claim_unverified"      # still neutral, never reveals the review
    assert ticket_id not in r2["replies"][0]["text"] and r2["tickets"] == []   # no second ticket
    rows = claims(rt)
    assert len(rows) == 1 and rows[0].ticket_id == ticket_id and rows[0].attempts >= 2
    assert rows[0].result == "MISMATCH"


def test_same_message_delivered_twice_runs_once(settings, borrower):
    a, rt, post, p = setup(settings, borrower)
    rec = {"utr": p["utr"], "amount": p["amount"], "txn_date": p["txn_date"]}
    r1 = post(type="image", receipt=rec, message_id="wamid.DUP")
    r2 = post(type="image", receipt=rec, message_id="wamid.DUP")          # webhook retry
    assert r1["duplicate"] is False and r2["duplicate"] is True
    assert r2["replies"] == r1["replies"]                                  # same answer, nothing re-run
    rows = claims(rt)
    assert len(rows) == 1 and rows[0].attempts == 1                        # not counted twice


def test_confirmation_survives_restart(settings, borrower):
    """The replay guard lives in the database, not in memory."""
    from servicing_agent.platform.api.app import create_app
    a, rt, post, p = setup(settings, borrower)
    post(type="image", receipt={"utr": p["utr"], "amount": p["amount"], "txn_date": p["txn_date"]})
    restarted = create_app(settings).state.runtime                         # same database, fresh process state
    assert restarted.tools.payments.claimed_by(p["utr"]) == a["account_id"]
