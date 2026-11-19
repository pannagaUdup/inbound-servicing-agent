"""Full evidence in media/extractions; masked copies in audit_events."""

import base64

from fastapi.testclient import TestClient
from sqlalchemy import select

from servicing_agent.contracts.state import MediaEvidence
from servicing_agent.platform.db import AuditEvent, Extraction, Media


def rows(rt, model):
    with rt.session_factory() as s:
        return s.scalars(select(model)).all()


def verified_client(settings, borrower, perception=None):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(settings, perception=perception)
    client = TestClient(app)
    for t in ("hi", a["account_id"], "123456"):
        client.post("/v1/simulate/messages", json={"sender": sender, "text": t})
    return app.state.runtime, client, sender


def test_audit_events_never_hold_raw_otps_or_utrs(settings, borrower):
    rt, client, sender = verified_client(settings, borrower)
    client.post("/v1/simulate/messages", json={"sender": sender, "text": "paid UTR 123412341234"})
    texts = [e.data.get("text") or "" for e in rows(rt, AuditEvent) if e.type in ("inbound", "outbound")]
    assert "123456" not in " ".join(texts)                      # the OTP the borrower typed
    assert "123412341234" not in " ".join(texts)                # full UTR
    assert any("••••1234" in t for t in texts)


def test_worker_batch_media_is_recorded_in_full(settings, borrower):
    a, sender = borrower
    rt, client, _ = verified_client(settings, borrower)
    cid = rt.conversation_id_for(sender)
    p = next(p for p in rt.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    body = {"conversation_id": cid, "messages": [
        {"message_id": "wamid.I", "sender": sender, "type": "image", "media_id": "META123", "media_mime": "image/jpeg",
         "media": {"kind": "upi_receipt", "utr": p["utr"], "amount": p["amount"], "txn_date": p["txn_date"],
                   "confidence": 0.98, "provider": "sarvam-docai"}},
        {"message_id": "wamid.V", "sender": sender, "type": "voice", "media_id": "META456",
         "media": {"kind": "voice_note", "transcript": "I have paid", "original_transcript": "ನಾನು ಕಟ್ಟಿದ್ದೀನಿ",
                   "language": "kn", "confidence": 0.95, "provider": "sarvam-stt"}}]}
    client.post("/v1/turns/batched", json=body)
    client.post("/v1/turns/batched", json=body)                 # duplicate delivery adds nothing

    media = {m.id: m for m in rows(rt, Media)}
    assert media["wamid.I"].s3_key == "meta:META123" and media["wamid.V"].s3_key == "meta:META456"
    ex = {(e.media_id, e.kind): e for e in rows(rt, Extraction)}
    assert len(ex) == 2
    vlm = ex[("wamid.I", "VLM")]
    assert vlm.fields["utr"] == p["utr"] and vlm.confidence == 0.98 and vlm.model_version == "sarvam-docai"
    asr = ex[("wamid.V", "ASR")]
    assert asr.fields["original_transcript"] == "ನಾನು ಕಟ್ಟಿದ್ದೀನಿ" and asr.language == "kn"


class StubFilePerception:
    """Real Perception file handling (save + media row), fake extraction."""

    def __init__(self, store):
        from servicing_agent.data.perception.pipeline import Perception
        self._p = Perception(None, None, store)

    @property
    def recorder(self):
        return getattr(self._p, "recorder", None)

    @recorder.setter
    def recorder(self, value):
        self._p.recorder = value

    def image(self, raw, *, filename, message_id, conversation_id):
        self._p._save(raw, filename, message_id, conversation_id, "image")
        return MediaEvidence(kind="upi_receipt", utr="999999999999", amount=10.0, confidence=0.9)


def test_uploaded_file_gets_its_path_and_checksum(settings, borrower, tmp_path):
    import hashlib

    from servicing_agent.data.perception.media_store import MediaStore
    rt, client, sender = verified_client(settings, borrower, StubFilePerception(MediaStore(tmp_path)))
    data = b"\x89PNG audit"
    client.post("/v1/simulate/messages", json={"sender": sender, "type": "image", "message_id": "wamid.F",
                                               "media_base64": base64.b64encode(data).decode()})
    m = next(m for m in rows(rt, Media) if m.id == "wamid.F")
    assert m.s3_key.startswith(str(tmp_path)) and m.checksum == hashlib.sha256(data).hexdigest()
    assert any(e.media_id == "wamid.F" and e.kind == "VLM" for e in rows(rt, Extraction))


def test_translation_is_recorded_in_full(settings, borrower):
    from tests.test_normalizer import REPLIES, FakeChat

    from servicing_agent.agent.normalizer import SarvamNormalizer
    rt, client, sender = verified_client(settings, borrower)
    rt.normalizer = SarvamNormalizer(FakeChat(REPLIES))
    client.post("/v1/simulate/messages", json={"sender": sender, "text": "naanu ninne 10 saavira kattidini",
                                               "message_id": "wamid.T"})
    tr = next(e for e in rows(rt, Extraction) if e.kind == "TRANSLATION")
    assert tr.media_id == "wamid.T" and tr.model_version == "sarvam-105b-conversations"
    assert tr.fields["original"] == "naanu ninne 10 saavira kattidini"
    assert tr.fields["english"].startswith("I paid Rs 10000 yesterday")
