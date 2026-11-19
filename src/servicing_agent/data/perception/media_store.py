"""Keeps every uploaded image and voice note on disk, with what perception extracted from it.

Layout (local stand-in for S3; T2 swaps the backend, keeping the same save/record calls):

    <media_dir>/
      2026/11/19/
        wamid.HBgM123.png      the file as received
        wamid.HBgM123.json     metadata + extracted MediaEvidence

The raw file is written *before* calling Sarvam, so it's kept even if extraction fails.
Files contain real borrower data: the folder is git-ignored. Retention (90 days in the
design) is to be enforced by a cleanup job.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path

from servicing_agent.contracts.state import MediaEvidence

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".ogg", ".opus", ".wav", ".mp3", ".m4a", ".aac", ".amr"}


def _safe(name: str, max_len: int = 80) -> str:
    cleaned = _SAFE.sub("_", name).strip("._") or "file"
    return cleaned[:max_len]


class MediaStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, data: bytes, *, filename: str, message_id: str, conversation_id: str, kind: str,
             now: datetime | None = None) -> Path:
        now = now or datetime.now(timezone.utc)
        ext = Path(filename).suffix.lower()
        if ext not in _ALLOWED_EXT:
            ext = ".bin"
        folder = self.root / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_safe(message_id)}{ext}"
        # Never write outside the media root, whatever the IDs contain.
        if self.root.resolve() not in path.resolve().parents:
            raise ValueError("refusing to write outside the media directory")
        path.write_bytes(data)
        meta = {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "kind": kind,
            "original_filename": _safe(filename),
            "content_type": mimetypes.guess_type(f"x{ext}")[0],
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "received_at": now.isoformat(timespec="seconds"),
            "evidence": None,
        }
        path.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return path

    def record_evidence(self, media_path: Path, evidence: MediaEvidence) -> None:
        meta_path = media_path.with_suffix(".json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["evidence"] = evidence.model_dump(exclude_none=True)
        meta["extracted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
