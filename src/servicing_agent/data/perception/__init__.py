"""Perception (owner: T3): voice notes and images -> MediaEvidence.

* ``stub``    pre-extracted hints, used by /simulate and the eval harness
* ``sarvam``  HTTP client for Sarvam speech-to-text and Document AI
* ``stt``     voice note -> English transcript + original transcript + language
* ``receipt`` payment screenshot -> receipt fields with per-field confidence
"""

from servicing_agent.data.perception.stub import extract_receipt, transcribe_voice_note

__all__ = ["extract_receipt", "transcribe_voice_note"]
