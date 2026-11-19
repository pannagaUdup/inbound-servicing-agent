"""Redis-backed OTP generation + verification (owner: T1).

A real OTP: a cryptographically-random 6-digit code stored in Redis with a TTL, plus
an attempt counter for lockout. The code is never kept in the app session — only in
Redis — so it expires on its own and survives a worker restart.

Keys (per conversation/account):
    sa:otp:{key}      -> the code        (EXPIRE = ttl)
    sa:otp:att:{key}  -> attempt count   (EXPIRE = ttl)
"""

from __future__ import annotations

import logging
import secrets

import redis

logger = logging.getLogger(__name__)


def _code_key(key: str) -> str: return f"sa:otp:{key}"
def _att_key(key: str) -> str: return f"sa:otp:att:{key}"


class RedisOtpService:
    def __init__(self, redis_url: str, ttl_seconds: int = 300, max_attempts: int = 3):
        self.r = redis.from_url(redis_url, decode_responses=True)  # lazy connect
        self.ttl = ttl_seconds
        self.max = max_attempts

    def issue(self, key: str) -> str:
        """Generate, store (with TTL), and return a fresh 6-digit code. Resets attempts."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        pipe = self.r.pipeline()
        pipe.setex(_code_key(key), self.ttl, code)
        pipe.delete(_att_key(key))
        pipe.execute()
        logger.info("OTP issued key=%s ttl=%ds", key, self.ttl)
        return code

    def verify(self, key: str, code: str) -> tuple[str, int]:
        """Returns (status, attempts_left) where status is ok|wrong|expired|locked."""
        stored = self.r.get(_code_key(key))
        if stored is None:
            return ("expired", 0)
        attempts = self.r.incr(_att_key(key))
        self.r.expire(_att_key(key), self.ttl)
        left = max(0, self.max - attempts)
        if attempts > self.max:
            self.r.delete(_code_key(key), _att_key(key))
            return ("locked", 0)
        if (code or "").strip() == stored:
            self.r.delete(_code_key(key), _att_key(key))
            return ("ok", left)
        return ("wrong", left)

    def clear(self, key: str) -> None:
        self.r.delete(_code_key(key), _att_key(key))
