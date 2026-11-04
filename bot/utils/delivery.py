"""Bounded, observable delivery queues shared by both Telegram transports."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Hashable

logger = logging.getLogger(__name__)


@dataclass
class Job:
    key: Hashable
    run: Callable[[], Awaitable[None]]


class OrderedDispatcher:
    """One worker per account; bounded backlog and in-process duplicate suppression.

    A failed job is retained in diagnostics, not automatically replayed: a network
    timeout can mean Telegram accepted a send without returning its response.
    """

    def __init__(self, capacity: int = 500, history_size: int = 10000):
        self.capacity = capacity
        self.history_size = history_size
        self.queues: dict[int, asyncio.Queue] = {}
        self.workers: dict[int, asyncio.Task] = {}
        self.pending: dict[int, set] = {}
        self.completed: dict[int, OrderedDict] = {}
        self.failures: deque = deque(maxlen=100)
        self.delivered = 0
        self.duplicates = 0
        self.closing: set[int] = set()

    async def submit(self, account: int, key: Hashable, run: Callable) -> bool:
        if account in self.closing:
            raise RuntimeError("Delivery queue is shutting down")
        pending = self.pending.setdefault(account, set())
        history = self.completed.setdefault(account, OrderedDict())
        if key in pending or key in history:
            self.duplicates += 1
            return False
        queue = self.queues.setdefault(account, asyncio.Queue(self.capacity))
        if account not in self.workers:
            self.workers[account] = asyncio.create_task(self._work(account), name=f"delivery-{account}")
        pending.add(key)
        try:
            await queue.put(Job(key, run))
        except BaseException:
            pending.discard(key)
            raise
        return True

    async def _work(self, account: int) -> None:
        queue = self.queues[account]
        while True:
            job = await queue.get()
            try:
                await job.run()
                self.delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failures.append({"account": account, "key": repr(job.key),
                                      "error": type(exc).__name__, "time": time.time()})
                logger.exception("Delivery failed for account %s, key %r", account, job.key)
            finally:
                # Even failures must not be blindly resubmitted by the poller.
                history = self.completed[account]
                history[job.key] = None
                while len(history) > self.history_size:
                    history.popitem(last=False)
                self.pending[account].discard(job.key)
                queue.task_done()

    async def stop(self, account: int, timeout: float = 30) -> None:
        self.closing.add(account)
        queue = self.queues.get(account)
        try:
            if queue:
                try:
                    await asyncio.wait_for(queue.join(), timeout)
                except asyncio.TimeoutError:
                    logger.error("Shutdown timeout: account %s has %s queued deliveries", account, queue.qsize())
            task = self.workers.pop(account, None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            self.queues.pop(account, None)
            self.pending.pop(account, None)
            self.closing.discard(account)

    def snapshot(self, account: int | None = None) -> dict:
        return {"queued": sum(q.qsize() for aid, q in self.queues.items() if account is None or aid == account),
                "workers": len(self.workers), "delivered": self.delivered,
                "duplicates": self.duplicates,
                "recent_failures": sum(1 for f in self.failures if account is None or f["account"] == account)}


async def send_with_retry(send: Callable[[], Awaitable], attempts: int = 3):
    """Retry only explicit rate-limit rejections; ambiguous network errors escape."""
    from telegram.error import RetryAfter
    from telethon.errors import FloodWaitError

    for attempt in range(attempts):
        try:
            return await send()
        except (RetryAfter, FloodWaitError) as exc:
            if attempt == attempts - 1:
                raise
            delay = exc.retry_after if isinstance(exc, RetryAfter) else exc.seconds
            if hasattr(delay, "total_seconds"):
                delay = delay.total_seconds()
            logger.warning("Telegram rate limit: waiting %s seconds", delay)
            await asyncio.sleep(float(delay) + 0.1)
