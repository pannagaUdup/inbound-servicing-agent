"""POST /v1/turns/batched: the batching worker's hand-off format."""

from fastapi.testclient import TestClient


def make(settings):
    from servicing_agent.platform.api.app import create_app
    app = create_app(settings)
    return app, TestClient(app)


def msg(mid, sender, **kw):
    return {"message_id": mid, "sender": sender, "received_at": "2026-11-19T14:09:02Z", **kw}


def batch(cid, *messages):
    return {"conversation_id": cid, "batched_at": "2026-11-19T14:09:03Z", "messages": list(messages)}


def verify(client, cid, sender, account):
    for i, text in enumerate(("hi", account, "123456")):
        r = client.post("/v1/turns/batched", json=batch(cid, msg(f"wamid.v{i}", sender, type="text", text=text)))
        assert r.status_code == 200, r.text
    return r.json()


def test_text_screenshot_text_batch_is_one_claim(settings, borrower):
    a, sender = borrower
    app, client = make(settings)
    cid = app.state.runtime.conversation_id_for(sender)
    assert verify(client, cid, sender, a["account_id"])["verified"]
    p = next(p for p in app.state.runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    body = batch(cid,
                 msg("wamid.1", sender, type="text", text="I have paid"),
                 msg("wamid.2", sender, type="image", text=None, media_id="4114351848871289", media_mime="image/jpeg",
                     media={"kind": "upi_receipt", "utr": p["utr"], "amount": p["amount"], "txn_date": p["txn_date"],
                            "payee_vpa": "collect@cnbank", "status": "success", "confidence": 0.98}),
                 msg("wamid.3", sender, type="text", text="pls check"))
    r = client.post("/v1/turns/batched", json=body).json()
    assert r["intents"] == ["payment_claim"]
    assert [x["template_id"] for x in r["replies"]] == ["claim_matched"]
    assert r["normalized"]["message_ids"] == ["wamid.1", "wamid.2", "wamid.3"]
    assert r["normalized"]["tasks"][0]["slots"]["media_id"] == "wamid.2"          # claim linked to its message

    again = client.post("/v1/turns/batched", json=body).json()                     # worker retries the batch
    assert again["duplicate"] is True and again["replies"] == r["replies"]


def test_voice_evidence_is_read_in_send_order(settings, borrower):
    a, sender = borrower
    app, client = make(settings)
    cid = app.state.runtime.conversation_id_for(sender)
    verify(client, cid, sender, a["account_id"])
    p = next(p for p in app.state.runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    r = client.post("/v1/turns/batched", json=batch(cid,
        msg("wamid.v", sender, type="voice", media_id="99", media_mime="audio/ogg",
            media={"kind": "voice_note", "transcript": "I have already paid", "confidence": 0.95}),
        msg("wamid.t", sender, type="text", text=f"UTR {p['utr']}"))).json()
    assert r["normalized"]["merged_text"].startswith("I have already paid")
    assert r["normalized"]["tasks"][0]["slots"]["source"] == "voice"
    assert r["replies"][0]["template_id"] == "claim_matched"


def test_rejects_unperceived_media_and_wrong_conversation(settings, borrower):
    _, sender = borrower
    app, client = make(settings)
    cid = app.state.runtime.conversation_id_for(sender)
    r = client.post("/v1/turns/batched", json=batch(cid, msg("m1", sender, type="image", media_id="123")))
    assert r.status_code == 422 and "run perception first" in r.text
    r = client.post("/v1/turns/batched", json=batch("wa:0000000000", msg("m2", sender, type="text", text="hi")))
    assert r.status_code == 422 and "conversation_id" in r.text
