"""Perception pipeline interface (owner: T3).

Turns raw voice notes and images into ``MediaEvidence`` before the agent sees them.
The stubs below accept pre-extracted fields (what /v1/simulate sends) so the rest
of the system works end to end; T3 replaces them with:

* speech-to-text: Sarvam Saarika / AI4Bharat IndicConformer, Whisper large-v3 fallback
* receipts: a VLM extraction to JSON + PaddleOCR cross-check
"""

from __future__ import annotations

from servicing_agent.contracts.state import MediaEvidence


def transcribe_voice_note(audio_bytes: bytes | None, *, hint_transcript: str | None = None) -> MediaEvidence:
    # TODO(T3): real STT. The hint lets synthetic conversations carry their ground-truth transcript.
    return MediaEvidence(kind="voice_note", transcript=hint_transcript or "", confidence=1.0 if hint_transcript else 0.0,
                         provider="stub")


def extract_receipt(image_bytes: bytes | None, *, hint: dict | None = None) -> MediaEvidence:
    # TODO(T3): VLM + OCR extraction with per-field confidence.
    hint = hint or {}
    return MediaEvidence(
        kind="upi_receipt",
        provider="stub",
        utr=hint.get("utr"),
        amount=hint.get("amount"),
        txn_date=hint.get("txn_date"),
        payee_vpa=hint.get("payee_vpa"),
        status=hint.get("status"),
        ocr_text=hint.get("ocr_text"),
        confidence=1.0 if hint else 0.0,
    )
