"""WhatsApp voice notes arrive as OGG/Opus (48 kHz). Sarvam speech-to-text takes 16 kHz audio."""

from __future__ import annotations

import shutil
import subprocess

MAX_SECONDS = 120


class AudioError(Exception):
    pass


def to_wav_16k_mono(audio: bytes, max_seconds: int = MAX_SECONDS) -> bytes:
    """Convert any ffmpeg-readable audio to 16 kHz mono 16-bit WAV, truncated to ``max_seconds``."""
    if shutil.which("ffmpeg") is None:
        raise AudioError("ffmpeg is not installed (macOS: brew install ffmpeg; Debian: apt install ffmpeg)")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-t", str(max_seconds),
         "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
        input=audio, capture_output=True, timeout=30,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise AudioError(f"ffmpeg could not convert the audio: {proc.stderr.decode(errors='replace')[:200]}")
    return proc.stdout
