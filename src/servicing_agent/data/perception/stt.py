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

    def _call(self, wav: bytes, mode: str) -> dict:
        return self.client.speech_to_text(wav, filename="audio.wav", mode=mode, model=self.model)

    def transcribe(self, audio: bytes, *, already_wav: bool = False) -> MediaEvidence:
        try:
            wav = audio if already_wav else self.convert(audio)
        except AudioError as exc:
            return MediaEvidence(kind="voice_note", transcript="", confidence=0.0, provider="sarvam-stt",
                                 error=f"audio_conversion: {exc}")

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_orig = pool.submit(self._call, wav, "transcribe")
            fut_en = pool.submit(self._call, wav, "translate")
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
