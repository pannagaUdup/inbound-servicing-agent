"""Thin HTTP client for Sarvam speech-to-text and Document AI.

One subscription key (``SARVAM_API_KEY``) covers both products. Only the endpoints and
fields confirmed by real responses are used:

* ``POST /speech-to-text`` -> ``{request_id, transcript, language_code, language_probability}``
* ``POST /doc-ai/v1/job/extract`` -> ``{job_id, status, run_id}``
* ``GET  /doc-ai/v1/job/{id}/status`` -> ``{status, ...}``
* ``GET  /doc-ai/v1/job/{id}/results?format=json`` -> ``{result, annotations, documents, ...}``
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import httpx

DEFAULT_BASE_URL = "https://api.sarvam.ai"
TERMINAL_STATUSES = {"completed", "partially_completed", "failed", "rejected"}


class SarvamError(Exception):
    """Any failure talking to Sarvam: HTTP error, timeout, bad response, failed job."""


class SarvamTimeout(SarvamError):
    pass


@dataclass
class SarvamConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    request_timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> "SarvamConfig":
        from servicing_agent.config import load_dotenv
        load_dotenv()
        key = os.environ.get("SARVAM_API_KEY", "").strip()
        if not key:
            raise SarvamError("SARVAM_API_KEY is not set. Put it in .env (never commit it).")
        return cls(api_key=key, base_url=os.environ.get("SA_SARVAM_BASE_URL", DEFAULT_BASE_URL))


class SarvamClient:
    def __init__(self, config: SarvamConfig, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep, clock=time.monotonic):
        self.config = config
        self._http = httpx.Client(
            base_url=config.base_url,
            headers={"api-subscription-key": config.api_key},
            timeout=config.request_timeout_s,
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock

    def close(self):
        self._http.close()

    # ---- helpers
    def _check(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            # Never include the request (it carries the key); the body is Sarvam's error message.
            raise SarvamError(f"Sarvam HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SarvamError(f"Sarvam returned non-JSON: {resp.text[:200]}") from exc

    def _request(self, method: str, url: str, **kw) -> dict:
        try:
            return self._check(self._http.request(method, url, **kw))
        except httpx.TimeoutException as exc:
            raise SarvamTimeout(f"Sarvam timed out on {url}") from exc
        except httpx.HTTPError as exc:
            raise SarvamError(f"Sarvam request failed on {url}: {exc}") from exc

    # ---- speech-to-text
    def speech_to_text(self, audio: bytes, *, filename: str = "audio.wav", mode: str = "transcribe",
                       model: str = "saaras:v4", language_code: str = "unknown",
                       sample_rate: int = 16000) -> dict:
        """mode: "transcribe" (original language) or "translate" (English)."""
        data = {"model": model, "language_code": language_code, "mode": mode, "sample_rate": str(sample_rate)}
        files = {"file": (filename, audio, "audio/wav" if filename.endswith(".wav") else "application/octet-stream")}
        body = self._request("POST", "/speech-to-text", data=data, files=files)
        if "transcript" not in body:
            raise SarvamError(f"speech-to-text response has no transcript: {list(body)}")
        return body

    # ---- Document AI
    def docai_submit(self, document: bytes, *, filename: str, schema: dict, language: str = "en-IN",
                     idempotency_key: str | None = None, classification: bool = False) -> str:
        data = {"schema": json.dumps(schema), "language": language, "output_format": "json",
                "classification": str(classification).lower(), "auto_orient": "true"}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        body = self._request("POST", "/doc-ai/v1/job/extract", data=data,
                             files={"file": (filename, document)}, headers=headers)
        job_id = body.get("job_id")
        if not job_id:
            raise SarvamError(f"Document AI submit returned no job_id: {body}")
        return job_id

    def docai_status(self, job_id: str) -> dict:
        return self._request("GET", f"/doc-ai/v1/job/{job_id}/status")

    def docai_results(self, job_id: str) -> dict:
        return self._request("GET", f"/doc-ai/v1/job/{job_id}/results", params={"format": "json"})

    def docai_extract(self, document: bytes, *, filename: str, schema: dict, budget_s: float = 8.0,
                      idempotency_key: str | None = None, language: str = "en-IN") -> dict:
        """Submit, poll with backoff inside ``budget_s``, then fetch results."""
        start = self._clock()
        job_id = self.docai_submit(document, filename=filename, schema=schema, language=language,
                                   idempotency_key=idempotency_key)
        delay = 0.3
        while True:
            status = self.docai_status(job_id).get("status")
            if status in TERMINAL_STATUSES:
                break
            if self._clock() - start + delay > budget_s:
                raise SarvamTimeout(f"Document AI job {job_id} still '{status}' after {budget_s:.0f}s")
            self._sleep(delay)
            delay = min(delay * 1.6, 1.5)
        if status in ("failed", "rejected"):
            raise SarvamError(f"Document AI job {job_id} {status}")
        return self.docai_results(job_id)
