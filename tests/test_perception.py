"""Sarvam perception clients, tested offline with httpx.MockTransport and synthetic fixtures."""

import base64
import copy
import json
from pathlib import Path

import httpx
import pytest

from servicing_agent.data.perception.pipeline import Perception
from servicing_agent.data.perception.receipt import (
    ReceiptExtractor, map_docai_result, normalize_amount, normalize_status, normalize_time, normalize_utr,
)
from servicing_agent.data.perception.sarvam import SarvamClient, SarvamConfig, SarvamError, SarvamTimeout
from servicing_agent.data.perception.stt import VoiceTranscriber

FIXTURE = json.loads((Path(__file__).parent / "fixtures/sarvam/docai_receipt.json").read_text())
KEY = "test-key-not-real"


def make_client(handler, clock=None):
    ticks = iter(range(0, 10_000))
    return SarvamClient(SarvamConfig(api_key=KEY), transport=httpx.MockTransport(handler),
                        sleep=lambda s: None, clock=clock or (lambda: next(ticks) * 0.5))


def docai_handler(results: dict, statuses=("pending", "completed"), seen: dict | None = None):
    state = {"polls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.setdefault("requests", []).append(req)
        if req.url.path == "/doc-ai/v1/job/extract":
            return httpx.Response(201, json={"job_id": "job-1", "status": "pending", "run_id": "run-1"})
        if req.url.path.endswith("/status"):
            s = statuses[min(state["polls"], len(statuses) - 1)]
            state["polls"] += 1
            return httpx.Response(200, json={"status": s})
        if req.url.path.endswith("/results"):
            return httpx.Response(200, json=results)
        return httpx.Response(404)
    return handler


# ---------------- normalizers

@pytest.mark.parametrize("raw,expected", [("9:45 pm", "21:45"), ("09:45 AM", "09:45"), ("12:05 am", "00:05"),
                                          ("12:30 pm", "12:30"), ("21:45", "21:45"), ("21:45:10", "21:45"),
                                          ("9.45 p.m.", "21:45"), ("25:00", None), ("soon", None)])
def test_normalize_time(raw, expected):
    assert normalize_time(raw) == expected


def test_normalize_other_fields():
    assert normalize_status("Completed") == "success"
    assert normalize_status("Payment Failed") is None      # unknown wording -> not guessed
    assert normalize_status("failed") == "failed"
    assert normalize_utr("1234 5678 9012") == "123456789012"
    assert normalize_utr("12345") is None
    assert normalize_amount("₹4,500.00") == 4500.0
    assert normalize_amount(33) == 33.0


# ---------------- Document AI mapping

def test_map_docai_result_from_real_shape():
    ev = map_docai_result(FIXTURE)
    assert ev.kind == "upi_receipt"                         # decided from UTR + amount, not document_type (0.13)
    assert (ev.utr, ev.amount, ev.txn_date, ev.txn_time) == ("123456789012", 4500.0, "2026-11-18", "21:45")
    assert ev.status == "success"
    assert ev.payee_vpa == "....ect@cnbank"                 # masked VPA kept; reconciliation handles it
    assert ev.field_confidence["utr"] == 1.0
    assert ev.low_confidence_fields == []
    assert ev.language == "en" and ev.provider == "sarvam-docai" and ev.error is None


def test_low_confidence_utr_is_not_used():
    body = copy.deepcopy(FIXTURE)
    body["annotations"]["utr"]["confidence"] = 0.6
    ev = map_docai_result(body)
    assert ev.utr is None and "utr" in ev.low_confidence_fields
    assert ev.kind == "upi_receipt"                         # still a receipt -> agent asks to type the UTR
    assert ev.confidence == 0.0


def test_non_receipt_image():
    body = {"result": {"document_type": "other"}, "annotations": {"document_type": {"confidence": 0.95}},
            "documents": [{"language": "en-IN"}]}
    ev = map_docai_result(body)
    assert ev.kind == "other" and ev.utr is None


# ---------------- Document AI client

def test_docai_extract_polls_then_returns_results():
    seen = {}
    client = make_client(docai_handler(FIXTURE, ("pending", "pending", "completed"), seen))
    ev = ReceiptExtractor(client).extract(b"\x89PNG fake", filename="r.png", message_id="wamid.1")
    assert ev.utr == "123456789012"
    submit = seen["requests"][0]
    assert submit.headers["api-subscription-key"] == KEY
    assert submit.headers["Idempotency-Key"] == "wamid.1"
    assert b'"utr"' in submit.content                       # schema was sent


def test_docai_timeout_returns_error_evidence():
    client = make_client(docai_handler(FIXTURE, ("pending",)))
    ev = ReceiptExtractor(client, budget_s=3).extract(b"img")
    assert ev.kind == "other" and ev.error.startswith("docai_failed") and ev.utr is None
    with pytest.raises(SarvamTimeout):
        make_client(docai_handler(FIXTURE, ("pending",))).docai_extract(b"img", filename="r.png", schema={}, budget_s=3)


def test_docai_failed_job_and_http_error():
    ev = ReceiptExtractor(make_client(docai_handler(FIXTURE, ("failed",)))).extract(b"img")
    assert "failed" in ev.error
    client = make_client(lambda req: httpx.Response(401, json={"error": "invalid key"}))
    with pytest.raises(SarvamError) as exc:
        client.docai_submit(b"img", filename="r.png", schema={})
    assert "401" in str(exc.value) and KEY not in str(exc.value)


# ---------------- speech-to-text

def stt_handler(fail_mode: str | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content
        mode = "translate" if b'name="mode"\r\n\r\ntranslate' in body else "transcribe"
        assert b"saaras:v4" in body and req.headers["api-subscription-key"] == KEY
        if mode == fail_mode:
            return httpx.Response(500, json={"error": "boom"})
        text = ("I have made a payment of 10,000 rupees this morning." if mode == "translate"
                else "ಇವತ್ತು ಬೆಳಿಗ್ಗೆ 10,000 ರೂಪಾಯಿ ಕಟ್ಟಿದ್ದೀನಿ.")
        return httpx.Response(200, json={"request_id": "r1", "transcript": text,
                                         "language_code": "kn-IN", "language_probability": 1.0})
    return handler


def test_stt_runs_transcribe_and_translate():
    ev = VoiceTranscriber(make_client(stt_handler())).transcribe(b"RIFFwav", already_wav=True)
    assert ev.kind == "voice_note" and ev.language == "kn" and ev.confidence == 1.0
    assert ev.transcript.startswith("I have made a payment")
    assert "10,000" in ev.original_transcript and ev.error is None


def test_stt_partial_failure_falls_back():
    ev = VoiceTranscriber(make_client(stt_handler("translate"))).transcribe(b"wav", already_wav=True)
    assert "10,000" in ev.transcript and ev.error == "translate_failed"   # original text, normalizer can translate
    ev = VoiceTranscriber(make_client(stt_handler("transcribe"))).transcribe(b"wav", already_wav=True)
    assert ev.transcript.startswith("I have") and ev.original_transcript is None


def test_stt_conversion_failure():
    from servicing_agent.data.perception.audio import AudioError

    def bad_convert(_):
        raise AudioError("ffmpeg is not installed")
    ev = VoiceTranscriber(make_client(stt_handler()), convert=bad_convert).transcribe(b"ogg")
    assert ev.transcript == "" and ev.confidence == 0.0 and ev.error.startswith("audio_conversion")


# ---------------- end to end through the API

def test_media_store_saves_file_and_evidence(tmp_path):
    from servicing_agent.contracts.state import MediaEvidence
    from servicing_agent.data.perception.media_store import MediaStore
    store = MediaStore(tmp_path)
    path = store.save(b"\x89PNG data", filename="Receipt Final.PNG", message_id="../../etc/passwd",
                      conversation_id="wa:9000000000", kind="image")
    assert tmp_path.resolve() in path.resolve().parents        # stayed inside the media dir
    assert path.suffix == ".png" and path.read_bytes() == b"\x89PNG data"
    store.record_evidence(path, MediaEvidence(kind="upi_receipt", utr="123456789012"))
    meta = json.loads(path.with_suffix(".json").read_text())
    assert meta["conversation_id"] == "wa:9000000000" and meta["size_bytes"] == 9
    assert meta["evidence"]["utr"] == "123456789012" and len(meta["sha256"]) == 64


def test_api_real_receipt_image_matches_payment(settings, borrower, tmp_path):
    from fastapi.testclient import TestClient

    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app_holder = {}

    def results_for_store():
        store = app_holder["app"].state.runtime.store
        p = next(p for p in store.payments_by_account[a["account_id"]] if p["status"] == "success")
        body = copy.deepcopy(FIXTURE)
        body["result"].update(utr=p["utr"], amount=p["amount"], txn_date=p["txn_date"])
        body["annotations"]["txn_date"]["normalized_value"] = p["txn_date"]
        return body

    built = {}

    def handler(req):
        if "h" not in built:                     # build once, so the job's poll state persists
            built["h"] = docai_handler(results_for_store())
        return built["h"](req)

    from servicing_agent.data.perception.media_store import MediaStore
    perception = Perception.sarvam(make_client(handler), store=MediaStore(tmp_path / "media"))
    app = create_app(settings, perception=perception)
    app_holder["app"] = app
    client = TestClient(app)
    post = lambda **kw: client.post("/v1/simulate/messages", json={"sender": sender, **kw})
    post(text="already paid")
    post(text=a["account_id"])
    assert post(text="123456").json()["verified"]
    r = post(type="image", media_base64=base64.b64encode(b"\x89PNG fake").decode(), media_filename="r.png").json()
    assert r["replies"][0]["template_id"] == "claim_matched"   # masked payee VPA no longer blocks a genuine payment
    saved = list((tmp_path / "media").rglob("*.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"\x89PNG fake"
    meta = json.loads(saved[0].with_suffix(".json").read_text())
    assert meta["conversation_id"] == r["conversation_id"] and meta["evidence"]["kind"] == "upi_receipt"


def test_api_rejects_placeholder_sender_and_empty_media(settings, borrower):
    from fastapi.testclient import TestClient

    from servicing_agent.platform.api.app import create_app
    _, sender = borrower
    client = TestClient(create_app(settings))
    r = client.post("/v1/simulate/messages", json={"sender": "91XXXXXXXXXX", "text": "hi"})
    assert r.status_code == 422 and "10 digits" in r.text
    r = client.post("/v1/simulate/messages", json={"sender": sender, "type": "image", "media_base64": ""})
    assert r.status_code == 422 and "check the path" in r.text


def test_api_rejects_real_media_when_perception_is_off(settings, borrower):
    from fastapi.testclient import TestClient

    from servicing_agent.platform.api.app import create_app
    _, sender = borrower
    client = TestClient(create_app(settings))
    r = client.post("/v1/simulate/messages", json={"sender": sender, "type": "image",
                                                    "media_base64": base64.b64encode(b"x").decode()})
    assert r.status_code == 400 and "SA_PERCEPTION" in r.json()["detail"]
