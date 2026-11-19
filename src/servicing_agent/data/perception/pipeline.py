"""Entry point the API and (later) the queue worker use to turn raw media into MediaEvidence."""

from __future__ import annotations

import logging
from pathlib import Path

from servicing_agent.contracts.state import MediaEvidence
from servicing_agent.data.perception.media_store import MediaStore
from servicing_agent.data.perception.receipt import ReceiptExtractor
from servicing_agent.data.perception.sarvam import SarvamClient, SarvamConfig
from servicing_agent.data.perception.stt import VoiceTranscriber

log = logging.getLogger(__name__)


class PerceptionDisabled(Exception):
    pass


class Perception:
    def __init__(self, voice: VoiceTranscriber | None, receipt: ReceiptExtractor | None,
                 store: MediaStore | None = None):
        self._voice = voice
        self._receipt = receipt
        self.store = store

    @classmethod
    def sarvam(cls, client: SarvamClient | None = None, store: MediaStore | None = None) -> "Perception":
        client = client or SarvamClient(SarvamConfig.from_env())
        return cls(VoiceTranscriber(client), ReceiptExtractor(client), store)

    def _save(self, data: bytes, filename: str, message_id: str, conversation_id: str, kind: str) -> Path | None:
        if not self.store:
            return None
        try:
            return self.store.save(data, filename=filename, message_id=message_id,
                                   conversation_id=conversation_id, kind=kind)
        except OSError as exc:  # a full disk must not stop the borrower getting a reply
            log.error("could not store media %s: %s", message_id, exc)
            return None

    def _record(self, path: Path | None, evidence: MediaEvidence) -> None:
        if path and self.store:
            try:
                self.store.record_evidence(path, evidence)
            except OSError as exc:
                log.error("could not record evidence for %s: %s", path, exc)

    def voice(self, audio: bytes, *, filename: str = "voice.ogg", message_id: str = "msg",
              conversation_id: str = "unknown") -> MediaEvidence:
        if not self._voice:
            raise PerceptionDisabled("Real media needs SA_PERCEPTION=sarvam and SARVAM_API_KEY")
        path = self._save(audio, filename, message_id, conversation_id, "voice")
        evidence = self._voice.transcribe(audio, already_wav=filename.lower().endswith(".wav"))
        self._record(path, evidence)
        return evidence

    def image(self, image: bytes, *, filename: str = "receipt.png", message_id: str = "msg",
              conversation_id: str = "unknown") -> MediaEvidence:
        if not self._receipt:
            raise PerceptionDisabled("Real media needs SA_PERCEPTION=sarvam and SARVAM_API_KEY")
        path = self._save(image, filename, message_id, conversation_id, "image")
        evidence = self._receipt.extract(image, filename=filename, message_id=message_id)
        self._record(path, evidence)
        return evidence
