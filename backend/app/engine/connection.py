"""Keeps a broker connection alive: reconnects with exponential backoff."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

from app.brokers.base import BrokerAdapter, BrokerError

log = logging.getLogger(__name__)


class Backoff:
    def __init__(
        self, initial: float = 0.5, maximum: float = 30.0, factor: float = 2.0, jitter: float = 0.1
    ) -> None:
        self.initial = initial
        self.maximum = maximum
        self.factor = factor
        self.jitter = jitter
        self.attempt = 0

    def next_delay(self) -> float:
        delay = min(self.maximum, self.initial * (self.factor ** self.attempt))
        self.attempt += 1
        if self.jitter:
            delay *= 1 + random.uniform(-self.jitter, self.jitter)
        return max(0.0, delay)

    def reset(self) -> None:
        self.attempt = 0


class ConnectionSupervisor:
    def __init__(
        self,
        adapter: BrokerAdapter,
        *,
        on_connected: Callable[[], Awaitable[None]],
        on_attempt_failed: Callable[[Exception, float, int], Awaitable[None]] | None = None,
        backoff: Backoff | None = None,
    ) -> None:
        self.adapter = adapter
        self._on_connected = on_connected
        self._on_attempt_failed = on_attempt_failed
        self.backoff = backoff or Backoff()
        self._task: asyncio.Task | None = None
        self._stopped = False

    @property
    def reconnecting(self) -> bool:
        return self._task is not None and not self._task.done()

    def ensure_connected(self) -> None:
        """Start a background reconnect loop unless one is running already."""
        if self._stopped or self.reconnecting or self.adapter.is_connected:
            return
        self._task = asyncio.create_task(self._loop(), name=f"reconnect-{self.adapter.account_id}")

    async def _loop(self) -> None:
        while not self._stopped and not self.adapter.is_connected:
            try:
                await self.adapter.connect()
            except BrokerError as exc:
                delay = self.backoff.next_delay()
                if self._on_attempt_failed:
                    await self._on_attempt_failed(exc, delay, self.backoff.attempt)
                await asyncio.sleep(delay)
                continue
            except Exception:  # unexpected adapter bug: keep trying, but slowly
                log.exception("Unexpected error while connecting %s", self.adapter.account_id)
                await asyncio.sleep(self.backoff.next_delay())
                continue
            self.backoff.reset()
            await self._on_connected()
            return

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def restart(self) -> None:
        self._stopped = False
