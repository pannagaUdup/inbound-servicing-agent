"""Live worker path: queue batch -> download -> perception -> one merged agent turn -> replies."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from servicing_agent.contracts.state import InboundMessage, MediaEvidence


class FakePerception:
    def __init__(self, receipts=None, transcript="I have paid"):
        self.receipts = receipts or {}          # media bytes -> receipt fields
        self.transcript = transcript
        self.calls = []

    def image(self, data, *, filename, message_id, conversation_id):
        self.calls.append(("image", filename, message_id))
        return MediaEvidence(kind="upi_receipt", confidence=0.99, provider="fake", **self.receipts[data])

    def voice(self, data, *, filename, message_id, conversation_id):
        self.calls.append(("voice", filename, message_id))
        return MediaEvidence(kind="voice_note", transcript=self.transcript, language="en", provider="fake")


def setup(settings, borrower, perception, downloads):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(settings, perception=perception)
    rt = app.state.runtime
    rt.channel.download_media = lambda media_id: (downloads[media_id], "image/jpeg")
    client = TestClient(app)
    for t in ("hi there", a["account_id"], "123456"):
        client.post("/v1/simulate/messages", json={"sender": sender, "text": t})
    rt.channel.sent.clear()
    return app, rt, sender


def msg(mid, sender, type="text", text=None, media_id=None):
    return InboundMessage(message_id=mid, sender=sender, type=type, text=text, media_id=media_id)


def replies(rt):
    """Text replies only; PDFs (receipts) go out as separate document messages."""
    return [m["payload"]["text"]["body"] for m in rt.channel.sent if "text" in m["payload"]]


def documents(rt):
    return [m["payload"]["document"]["filename"] for m in rt.channel.sent if m["payload"].get("type") == "document"]


def payment(rt, account_id):
    return next(p for p in rt.store.payments_by_account[account_id] if p["status"] == "success")


def test_text_screenshot_text_is_one_turn_with_one_answer(settings, borrower):
    a, _ = borrower
    from servicing_agent.platform.api.app import create_app  # noqa: F401
    perception = FakePerception()
    app, rt, sender = setup(settings, borrower, perception, {"M1": b"IMG1"})
    p = payment(rt, a["account_id"])
    perception.receipts[b"IMG1"] = {"utr": p["utr"], "amount": p["amount"], "txn_date": p["txn_date"]}

    app.state.process_batch([msg("w1", sender, text="I have paid"),
                             msg("w2", sender, type="image", media_id="M1"),
                             msg("w3", sender, text="pls check")])
    out = replies(rt)
    assert len(out) == 1 and "confirmed your payment" in out[0]          # one answer, not three
    assert len(documents(rt)) == 1 and documents(rt)[0].endswith(".pdf")  # plus the receipt PDF
    assert perception.calls == [("image", "M1.jpg", "w2")]


def test_two_screenshots_are_perceived_and_claimed(settings, borrower):
    a, _ = borrower
    perception = FakePerception()
    app, rt, sender = setup(settings, borrower, perception, {"M1": b"A", "M2": b"B"})
    pays = [x for x in rt.store.payments_by_account[a["account_id"]] if x["status"] == "success"]
    if len(pays) < 2:
        pytest.skip("borrower has fewer than two payments")
    perception.receipts.update({b"A": {"utr": pays[0]["utr"], "amount": pays[0]["amount"], "txn_date": pays[0]["txn_date"]},
                                b"B": {"utr": pays[1]["utr"], "amount": pays[1]["amount"], "txn_date": pays[1]["txn_date"]}})
    app.state.process_batch([msg("w1", sender, type="image", media_id="M1"),
                             msg("w2", sender, type="image", media_id="M2")])
    assert sum("confirmed your payment" in r for r in replies(rt)) == 2


def test_download_failure_asks_for_the_utr(settings, borrower):
    app, rt, sender = setup(settings, borrower, FakePerception(), {})
    def boom(media_id):
        raise RuntimeError("meta 500")
    rt.channel.download_media = boom
    app.state.process_batch([msg("w1", sender, text="paid"), msg("w2", sender, type="image", media_id="MX")])
    assert "12-digit UTR" in replies(rt)[-1]


def test_voice_note_is_transcribed_and_claimed(settings, borrower):
    a, _ = borrower
    perception = FakePerception(transcript="I have already paid")
    app, rt, sender = setup(settings, borrower, perception, {"V1": b"OGG"})
    p = payment(rt, a["account_id"])
    app.state.process_batch([msg("w1", sender, type="voice", media_id="V1"),
                             msg("w2", sender, text=f"UTR {p['utr']}")])
    assert "confirmed your payment" in replies(rt)[-1]
    assert perception.calls[0][0] == "voice"


def test_button_taps_are_handled_on_their_own(settings, borrower):
    app, rt, sender = setup(settings, borrower, FakePerception(), {})
    seen = []
    rt.inbound.handle = lambda m: seen.append((m.type, m.text)) or None
    app.state.process_batch([msg("w1", sender, text="hi"), msg("w2", sender, type="button", text="PAY_INFO"),
                             msg("w3", sender, text="also my emi")])
    assert seen == [("text", "hi"), ("button", "PAY_INFO"), ("text", "also my emi")]


# ---------------- Meta webhook shapes

def _wrap(m):
    return {"entry": [{"changes": [{"value": {"messages": [m]}}]}]}


def test_screenshot_sent_as_a_file_with_caption_is_an_image():
    from servicing_agent.platform.channel import parse_inbound_webhook
    m = parse_inbound_webhook(_wrap({"from": "919000000001", "id": "w1", "type": "document",
                                     "document": {"id": "D1", "mime_type": "image/png", "caption": "I have paid"}}))[0]
    assert (m.type, m.text, m.media_id, m.media_mime) == ("image", "I have paid", "D1", "image/png")


def test_photo_caption_and_pdf_and_other_files():
    from servicing_agent.platform.channel import parse_inbound_webhook
    photo = parse_inbound_webhook(_wrap({"from": "9190", "id": "w2", "type": "image",
                                         "image": {"id": "I1", "mime_type": "image/jpeg", "caption": "paid"}}))[0]
    pdf = parse_inbound_webhook(_wrap({"from": "9190", "id": "w3", "type": "document",
                                       "document": {"id": "P1", "mime_type": "application/pdf"}}))[0]
    docx = parse_inbound_webhook(_wrap({"from": "9190", "id": "w4", "type": "document",
                                        "document": {"id": "X1", "mime_type": "application/msword"}}))[0]
    assert (photo.type, photo.text) == ("image", "paid")
    assert pdf.type == "image" and docx.type == "text"


def test_file_screenshot_with_caption_is_matched_end_to_end(settings, borrower):
    """The exact case from WhatsApp: a PNG sent as a file with caption 'I have paid'."""
    from servicing_agent.platform.channel import parse_inbound_webhook
    a, _ = borrower
    perception = FakePerception()
    app, rt, sender = setup(settings, borrower, perception, {"D1": b"PNG"})
    p = payment(rt, a["account_id"])
    perception.receipts[b"PNG"] = {"utr": p["utr"], "amount": p["amount"], "txn_date": p["txn_date"]}
    msgs = parse_inbound_webhook(_wrap({"from": sender, "id": "wf", "type": "document",
                                        "document": {"id": "D1", "mime_type": "image/png", "caption": "I have paid"}}))
    app.state.process_batch(msgs)
    assert "confirmed your payment" in replies(rt)[-1]


# ---------------- holding reply

class FakeRedis:
    """Just enough of redis.asyncio for _process_conversation."""

    def __init__(self, first_look, drained):
        self.first_look, self.drained = first_look, list(drained)

    async def lrange(self, key, a, b):
        return self.first_look

    async def lpop(self, key):
        return self.drained.pop(0) if self.drained else None

    async def srem(self, *a):
        return 1

    async def llen(self, key):
        return 0


def run_conversation(first_look, drained):
    from servicing_agent.platform.queue import InboundQueue
    holds, processed = [], []
    q = InboundQueue("redis://unused", process_batch=processed.append, send_holding=holds.append, batch_seconds=0)
    q.r = FakeRedis([m.model_dump_json() for m in first_look], [m.model_dump_json() for m in drained])
    asyncio.run(q._process_conversation("wa:9000000001"))
    return holds, processed


def test_holding_reply_for_media_that_arrives_during_the_window():
    s = "919000000001"
    text, image = msg("t", s, text="I paid"), msg("i", s, type="image", media_id="M")
    holds, processed = run_conversation(first_look=[text], drained=[text, image])
    assert holds == [s] and len(processed[0]) == 2


def test_holding_reply_is_sent_once_per_batch():
    s = "919000000001"
    image = msg("i", s, type="image", media_id="M")
    holds, _ = run_conversation(first_look=[image], drained=[image])
    assert holds == [s]


def test_no_holding_reply_for_text_only():
    s = "919000000001"
    holds, _ = run_conversation(first_look=[msg("t", s, text="hi")], drained=[msg("t", s, text="hi")])
    assert holds == []
