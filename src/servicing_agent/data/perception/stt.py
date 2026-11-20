"""Voice note -> MediaEvidence via Sarvam speech-to-text.

Runs ``mode=transcribe`` (borrower's own words, for audit and number checks) and
``mode=translate`` (English, for the planner) in parallel, so the second call adds no wait.
Any number taken from a voice note must still be confirmed back to the borrower (T1).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from servicing_agent.contracts.state import MediaEvidence
from servicing_agent.data.perception.audio import AudioError, to_wav_16k_mono
from servicing_agent.data.perception.sarvam import SarvamClient, SarvamError

log = logging.getLogger(__name__)


AUDIO_MIME = {"wav": "audio/wav", "ogg": "audio/ogg", "mp3": "audio/mpeg", "m4a": "audio/mp4",
              "webm": "audio/webm", "aac": "audio/aac", "flac": "audio/flac", "amr": "audio/amr"}


def audio_filename(audio: bytes) -> str:
    """Name the upload after the real format (from its magic bytes), so Sarvam decodes it correctly.
    WhatsApp voice notes are Ogg/Opus."""
    head = audio[:16]
    if head.startswith(b"RIFF") and audio[8:12] == b"WAVE":
        return "audio.wav"
    if head.startswith(b"OggS"):
        return "audio.ogg"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio.mp3"
    if audio[4:8] == b"ftyp":
        return "audio.m4a"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio.webm"
    if head.startswith(b"fLaC"):
        return "audio.flac"
    if head.startswith(b"#!AMR"):
        return "audio.amr"
    if head[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return "audio.aac"
    return "audio.ogg"                       # unknown: WhatsApp's own format is the best guess


def short_language(code: str | None) -> str | None:
    """'kn-IN' -> 'kn'. Sarvam returns BCP-47 style codes; templates use the short form."""
    if not code or code == "unknown":
        return None
    return code.split("-")[0].lower()


class VoiceTranscriber:
    def __init__(self, client: SarvamClient, convert=to_wav_16k_mono, model: str = "saaras:v4"):
        self.client = client
        self.convert = convert
        self.model = model

    def _call(self, wav: bytes, mode: str, filename: str = "audio.wav") -> dict:
        return self.client.speech_to_text(wav, filename=filename, mode=mode, model=self.model)

    def transcribe(self, audio: bytes, *, already_wav: bool = False) -> MediaEvidence:
        filename = "audio.wav"
        try:
            wav = audio if already_wav else self.convert(audio)
        except AudioError as exc:
            # ffmpeg not available (or conversion failed): Sarvam STT accepts ogg/opus/mp3/m4a
            # directly, so send the original bytes, named after their real format.
            wav, filename = audio, audio_filename(audio)
            log.warning("audio conversion unavailable (%s); sending original audio to Sarvam as %s", exc, filename)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_orig = pool.submit(self._call, wav, "transcribe", filename)
            fut_en = pool.submit(self._call, wav, "translate", filename)
            results: dict[str, dict | Exception] = {}
            for name, fut in (("transcribe", fut_orig), ("translate", fut_en)):
                try:
                    results[name] = fut.result()
                except SarvamError as exc:
                    log.warning("Sarvam %s failed: %s", name, exc)
                    results[name] = exc

        orig, en = results["transcribe"], results["translate"]
        if isinstance(orig, Exception) and isinstance(en, Exception):
            return MediaEvidence(kind="voice_note", transcript="", confidence=0.0, provider="sarvam-stt",
                                 error=f"stt_failed: {en}")

        ok = en if not isinstance(en, Exception) else orig
        language = short_language(ok.get("language_code"))
        prob = ok.get("language_probability")
        confidence = float(prob) if isinstance(prob, (int, float)) else 0.5
        errors = [f"{name}_failed" for name, r in results.items() if isinstance(r, Exception)]

        return MediaEvidence(
            kind="voice_note",
            # Planner reads English. If translation failed, fall back to the original text;
            # the normalize step (T1) can translate it with the chat model.
            transcript=(en["transcript"] if not isinstance(en, Exception) else orig["transcript"]).strip(),
            original_transcript=None if isinstance(orig, Exception) else orig["transcript"].strip(),
            language=language,
            confidence=confidence,
            provider="sarvam-stt",
            error=", ".join(errors) or None,
        )
