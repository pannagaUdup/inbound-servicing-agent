"""Scheduled payment-claim rechecks that survive a restart.

Each job is a member of a Redis sorted set scored by its due time (epoch seconds). A poller
thread takes due jobs, claims each with ZREM (only one worker wins it, so a job runs once even
with several API processes) and runs it. A recheck may schedule the next one.

Without Redis (tests, or Redis down) jobs fall back to in-process timers; those are lost on a
restart, which is logged.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Callable

log = logging.getLogger(__name__)

KEY = "sa:claim_rechecks"


class RecheckScheduler:
    def __init__(self, run: Callable[[dict], None], redis_client=None, poll_seconds: float = 2.0):
        self.run = run
        self.r = redis_client
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def from_url(cls, redis_url: str, run: Callable[[dict], None], **kw) -> "RecheckScheduler":
        try:
            import redis
            client = redis.from_url(redis_url, decode_responses=True)
            client.ping()
        except Exception as exc:  # noqa: BLE001
            log.warning("recheck scheduler: Redis unavailable (%s); using in-process timers", exc)
            client = None
        return cls(run, client, **kw)

    @property
    def durable(self) -> bool:
        return self.r is not None

    # ---- scheduling
    def schedule(self, job: dict, delay_seconds: float) -> None:
        job = {**job, "job_id": job.get("job_id") or uuid.uuid4().hex}
        if self.r is not None:
            try:
                self.r.zadd(KEY, {json.dumps(job, sort_keys=True): time.time() + float(delay_seconds)})
                return
            except Exception:  # noqa: BLE001
                log.exception("recheck scheduler: Redis write failed; using an in-process timer")
        t = threading.Timer(float(delay_seconds), self._safe_run, args=(job,))
        t.daemon = True
        t.start()

    def pending(self) -> int:
        return int(self.r.zcard(KEY)) if self.r is not None else 0

    # ---- polling
    def start(self) -> None:
        if self.r is None or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="claim-rechecks", daemon=True)
        self._thread.start()
        log.info("recheck scheduler started (%d pending)", self.pending())

    def stop(self) -> None:
        self._stop.set()

    def run_due(self, now: float | None = None) -> int:
        """Run every job that is due. Returns how many ran (used by the loop and by tests)."""
        if self.r is None:
            return 0
        due = self.r.zrangebyscore(KEY, 0, now if now is not None else time.time(), start=0, num=50)
        ran = 0
        for member in due:
            if self.r.zrem(KEY, member):          # claim it: only one poller gets 1
                self._safe_run(json.loads(member))
                ran += 1
        return ran

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception:  # noqa: BLE001
                log.exception("recheck scheduler poll failed")
            self._stop.wait(self.poll_seconds)

    def _safe_run(self, job: dict) -> None:
        try:
            self.run(job)
        except Exception:  # noqa: BLE001
            log.exception("claim recheck job failed: %s", job.get("job_id"))
