"""Per-conversation ordering and multi-media turns."""

import base64
import threading
import time

import pytest
from fastapi.testclient import TestClient

from servicing_agent.agent.planner import RulePlanner
from servicing_agent.contracts.state import InboundMessage, Intent, MediaEvidence


def receipt(utr=None, amount=None, date="2026-11-10"):
    return MediaEvidence(kind="upi_receipt", utr=utr, amount=amount, txn_date=date)


# ---------------- contract: merge_batch

def test_merge_batch_keeps_order_text_and_every_media_item():
    a = InboundMessage(message_id="m1", sender="919000000001", text="already paid")
    b = InboundMessage(message_id="m2", sender="919000000001", type="image", media=receipt("111111111111", 500))
    c = InboundMessage(message_id="m3", sender="919000000001", type="voice",
                       media=MediaEvidence(kind="voice_note", transcript="please check"))
    d = InboundMessage(message_id="m4", sender="919000000001", text="  pls check  ")
    merged = InboundMessage.merge_batch([a, b, c, d])
    assert merged.message_id == "m1" and merged.batched_message_ids == ["m1", "m2", "m3", "m4"]
    assert merged.text == "already paid\npls check"
    assert [ev.kind for ev in merged.all_media()] == ["upi_receipt", "voice_note"]
    assert merged.type == "image"
    assert InboundMessage.merge_batch([a]) is a
    with pytest.raises(ValueError):
        InboundMessage.merge_batch([a, InboundMessage(message_id="x", sender="919999999999", text="hi")])


# ---------------- planner: one claim per receipt

@pytest.mark.parametrize("text,media,expected_utrs", [
    ("already paid", [receipt("111111111111", 500)], ["111111111111"]),                     # text + image = one claim
    ("", [receipt("111111111111", 500), receipt("222222222222", 700)], ["111111111111", "222222222222"]),
    ("", [receipt("111111111111", 500), receipt("111111111111", 500)], ["111111111111"]),   # same screenshot twice
    ("paid, UTR 333333333333", [receipt("111111111111", 500)], ["111111111111", "333333333333"]),
    ("paid, UTR 333333333333", [receipt(None, 500)], ["333333333333"]),                     # typed UTR fills a gap
])
def test_one_payment_claim_per_receipt(text, media, expected_utrs):
    tasks = RulePlanner().plan(text, media)
    claims = [t for t in tasks if t.intent == Intent.PAYMENT_CLAIM]
    assert [t.slots["utr"] for t in claims] == expected_utrs


def test_other_intents_survive_alongside_receipts():
    tasks = RulePlanner().plan("paid, also send statement", [receipt("111111111111", 500)])
    assert [t.intent for t in tasks] == [Intent.PAYMENT_CLAIM, Intent.STATEMENT_REQUEST]


def test_single_media_argument_still_works():
    tasks = RulePlanner().plan("", receipt("111111111111", 500))
    assert tasks[0].intent == Intent.PAYMENT_CLAIM and tasks[0].slots["utr"] == "111111111111"


# ---------------- ordering: a slow screenshot is not overtaken

class SlowImagePerception:
    def image(self, raw, **kw):
        time.sleep(0.4)                                  # Document AI taking its time
        return receipt("999999999999", 100)

    def voice(self, raw, **kw):
        return MediaEvidence(kind="voice_note", transcript="")


def test_messages_from_one_borrower_are_handled_in_send_order(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    _, sender = borrower
    app = create_app(settings, perception=SlowImagePerception())
    client = TestClient(app)
    img = {"sender": sender, "type": "image", "media_base64": base64.b64encode(b"png").decode()}
    txt = {"sender": sender, "text": "pls check"}

    t_img = threading.Thread(target=lambda: client.post("/v1/simulate/messages", json=img))
    t_img.start()
    time.sleep(0.1)                                      # text is sent after the screenshot...
    t_txt = threading.Thread(target=lambda: client.post("/v1/simulate/messages", json=txt))
    t_txt.start()
    t_img.join(); t_txt.join()

    convo = app.state.runtime.conversations.get(app.state.runtime.conversation_id_for(sender))
    borrower_turns = [e["type"] for e in convo["transcript"] if e["role"] == "borrower"]
    assert borrower_turns == ["image", "text"]           # ...and is handled after it, despite being faster


# ---------------- batched turn endpoint

def verify(client, account, sender):
    for text in ("hi", account["account_id"], "123456"):
        r = client.post("/v1/simulate/messages", json={"sender": sender, "text": text}).json()
    assert r["verified"]


def test_batched_turn_text_plus_screenshot_gives_one_answer(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(settings)
    client = TestClient(app)
    verify(client, a, sender)
    p = next(p for p in app.state.runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    r = client.post("/v1/simulate/turn", json={"messages": [
        {"sender": sender, "text": "already paid"},
        {"sender": sender, "type": "image", "receipt": {"utr": p["utr"], "amount": p["amount"], "txn_date": p["txn_date"]}},
        {"sender": sender, "text": "pls check"},
    ]}).json()
    assert r["intents"] == ["payment_claim"]
    assert [x["template_id"] for x in r["replies"]] == ["claim_matched"]   # no "share UTR", no "didn't get that"


def test_batched_turn_two_screenshots_two_claims(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(settings)
    client = TestClient(app)
    verify(client, a, sender)
    pays = [p for p in app.state.runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success"]
    if len(pays) < 2:
        pytest.skip("borrower has fewer than two successful payments")
    msgs = [{"sender": sender, "type": "image", "receipt": {"utr": p["utr"], "amount": p["amount"], "txn_date": p["txn_date"]}}
            for p in pays[:2]]
    r = client.post("/v1/simulate/turn", json={"messages": msgs}).json()
    assert r["intents"] == ["payment_claim", "payment_claim"]
    assert [x["template_id"] for x in r["replies"]] == ["claim_matched", "claim_matched"]


def test_batched_turn_rejects_mixed_senders(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    _, sender = borrower
    client = TestClient(create_app(settings))
    r = client.post("/v1/simulate/turn", json={"messages": [{"sender": sender, "text": "a"},
                                                            {"sender": "919111111111", "text": "b"}]})
    assert r.status_code == 422
