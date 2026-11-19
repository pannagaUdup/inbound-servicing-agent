"""Evidence audit: every perceived item and every translation, in full, in Postgres (owner: T2).

* ``media``       one row per uploaded file: where it is (local path now, S3 key later), kind, SHA-256
* ``extractions`` what was read from it, in full and unmasked:
    - ``VLM``          screenshot fields (UTR, amount, date, payee, status) with per-field confidence
    - ``ASR``          voice note: English transcript + the borrower's own words + language
    - ``TRANSLATION``  typed regional text: original, English, model, numbers removed, amounts/dates checked
                       (media_id is the turn's message id; there is no file)

These two tables hold exact borrower words, so restrict who can read them. The broadly read
``audit_events`` log keeps masked copies only. Writes are idempotent per (media_id, kind): a
duplicate delivery never adds rows; a new upload of the same screenshot is a new message and
gets its own rows.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from servicing_agent.contracts.state import MediaEvidence
from servicing_agent.platform.db import Extraction, Media

log = logging.getLogger(__name__)


def media_key(message_id: str) -> str:
    return message_id[:64]                       # media.id is 64 chars; wamids fit


def extraction_kind(ev: MediaEvidence) -> str:
    return "ASR" if ev.kind == "voice_note" else "VLM"


class EvidenceRecorder:
    def __init__(self, session_factory):
        self.Session = session_factory

    def media(self, message_id: str, *, location: str, kind: str, checksum: str | None = None) -> None:
        """Insert the media row once; a later call with the real file location fills it in."""
        mid = media_key(message_id)
        try:
            with self.Session.begin() as s:
                row = s.get(Media, mid)
                if row is None:
                    s.add(Media(id=mid, s3_key=location[:256], kind=kind[:32], checksum=checksum))
                elif location and not row.s3_key.startswith(("data/", "/", "s3://")):
                    row.s3_key, row.checksum = location[:256], checksum or row.checksum
        except IntegrityError:
            pass                                 # a concurrent writer inserted it first

    def extraction(self, message_id: str, *, kind: str, fields: dict, language: str | None,
                   confidence: float, model: str | None) -> None:
        mid = media_key(message_id)
        try:
            with self.Session.begin() as s:
                if s.scalar(select(Extraction.id).where(Extraction.media_id == mid, Extraction.kind == kind)):
                    return
                s.add(Extraction(media_id=mid, kind=kind, language=(language or None) and language[:8],
                                 fields=fields, confidence=confidence, model_version=(model or None) and model[:64]))
        except IntegrityError:
            pass

    # ---- what the agent calls once per turn
    def record_media_evidence(self, ev: MediaEvidence, *, fallback_message_id: str,
                              media_handle: str | None = None) -> None:
        mid = ev.source_message_id or fallback_message_id
        location = f"meta:{media_handle}" if media_handle else "unstored"
        self.media(mid, location=location, kind=ev.kind)
        fields = ev.model_dump(exclude_none=True, exclude={"confidence", "provider", "source_message_id"})
        self.extraction(mid, kind=extraction_kind(ev), fields=fields, language=ev.language,
                        confidence=ev.confidence, model=ev.provider)

    def record_translation(self, message_id: str, translation: dict, original: str) -> None:
        fields = {"original": original, **translation}
        self.extraction(message_id, kind="TRANSLATION", fields=fields, language=translation.get("language"),
                        confidence=1.0, model=translation.get("model"))
