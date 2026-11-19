"""Async backbone (owner: T1): Redis-Streams ingress + worker pool.

Matches docs/ARCHITECTURE.md §1/§3:
  * Ingress only acks + enqueues (the webhook returns in well under 100 ms).
  * Partitioned by conversation: each conversation has its own ordered list; a
    per-conversation lock means only one worker touches a conversation at a time.
  * 3-second batching: messages that arrive within `batch_seconds` are drained and
    processed together, in order (coalesced split bubbles).
  * Holding reply under 3 s for voice/image, sent as soon as a worker picks the
    conversation, before the batch window.
  * Durable inbound log in a Redis Stream (`sa:inbound`) for audit/replay.

If Redis is unreachable the caller falls back to synchronous processing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

import redis.asyncio as aioredis

from servicing_agent.contracts.state import InboundMessage
from servicing_agent.data.identifiers import normalize_mobile

logger = logging.getLogger(__name__)

STREAM = "sa:inbound"       # durable inbound log (Redis Stream)
READY = "sa:ready"          # list of conversation ids with work waiting
ACTIVE = "sa:active"        # set guard so a conversation is enqueued to READY once


def conversation_id_for(sender: str) -> str:
    return f"wa:{normalize_mobile(sender)}"


def _conv(cid: str) -> str: return f"sa:conv:{cid}"
def _lock(cid: str) -> str: return f"sa:lock:{cid}"
def _seen(mid: str) -> str: return f"sa:seen:{mid}"


class InboundQueue:
    def __init__(self, redis_url: str, *, process_batch: Callable[[list[InboundMessage]], None],
                 send_holding: Callable[[str], None], workers: int = 4, batch_seconds: float = 3.0,
                 lock_ms: int = 30000):
        self.url = redis_url
        self.process_batch = process_batch
        self.send_holding = send_holding
        self.n = max(1, workers)
        self.batch = batch_seconds
        self.lock_ms = lock_ms
        self.r: aioredis.Redis | None = None
        self._tasks: list[asyncio.Task] = []

    async def connect(self) -> None:
        self.r = aioredis.from_url(self.url, decode_responses=True)
        await self.r.ping()

    async def enqueue(self, msg: InboundMessage) -> bool:
        """Append to the conversation's ordered list; mark it ready. Dedupe by message id.
        Returns False if this message id was already seen (WhatsApp retries)."""
        assert self.r is not None
        if msg.message_id:
            first = await self.r.set(_seen(msg.message_id), "1", nx=True, ex=86400)
            if not first:
                return False
        cid = conversation_id_for(msg.sender)
        payload = msg.model_dump_json()
        entry_id = await self.r.xadd(STREAM, {"cid": cid, "msg": payload}, maxlen=10000, approximate=True)
        await self.r.rpush(_conv(cid), payload)
        if await self.r.sadd(ACTIVE, cid):          # newly active -> schedule once
            await self.r.rpush(READY, cid)
        logger.info("ENQUEUE cid=%s type=%s id=%s entry=%s payload=%s",
                    cid, msg.type, msg.message_id, entry_id, payload)
        return True

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._worker(i)) for i in range(self.n)]
        logger.info("async backbone: %d workers, %.1fs batch window", self.n, self.batch)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        if self.r is not None:
            await self.r.aclose()

    async def _worker(self, i: int) -> None:
        assert self.r is not None
        while True:
            try:
                # Non-blocking poll (avoids BRPOP vs socket-timeout races across redis-py versions).
                cid = await self.r.lpop(READY)
                if cid is None:
                    await asyncio.sleep(0.25)
                    continue
                if not await self.r.set(_lock(cid), "1", nx=True, px=self.lock_ms):
                    # another worker owns this conversation; try again shortly
                    await asyncio.sleep(0.2)
                    await self.r.rpush(READY, cid)
                    continue
                try:
                    await self._process_conversation(cid)
                finally:
                    await self.r.delete(_lock(cid))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the worker alive
                logger.exception("worker %d error", i)
                await asyncio.sleep(0.5)

    async def _process_conversation(self, cid: str) -> None:
        assert self.r is not None
        # Holding reply within ~3s: if media is already waiting, acknowledge immediately.
        waiting = await self.r.lrange(_conv(cid), 0, -1)
        if waiting:
            msgs0 = [InboundMessage.model_validate_json(x) for x in waiting]
            if any(m.type in ("image", "voice") for m in msgs0):
                logger.info("HOLDING cid=%s (media present) -> sending holding reply", cid)
                await asyncio.to_thread(self.send_holding, msgs0[0].sender)
        # Batch window: let split bubbles / further messages arrive.
        await asyncio.sleep(self.batch)
        # Drain in order (LPOP loop catches anything pushed during the window too).
        msgs: list[InboundMessage] = []
        while True:
            x = await self.r.lpop(_conv(cid))
            if x is None:
                break
            msgs.append(InboundMessage.model_validate_json(x))
        await self.r.srem(ACTIVE, cid)
        if msgs:
            logger.info("PROCESS cid=%s batch=%d types=%s", cid, len(msgs), [m.type for m in msgs])
            await asyncio.to_thread(self.process_batch, msgs)
        # A message that landed between the final LPOP and SREM would be orphaned — re-schedule it.
        if await self.r.llen(_conv(cid)):
            if await self.r.sadd(ACTIVE, cid):
                await self.r.rpush(READY, cid)
