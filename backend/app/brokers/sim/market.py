"""Simulated market: one price per index family, random walk plus manual control."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Callable

from app.core.contracts import DEFAULT_PRICES, UNDERLYING_TICK, round_to_tick, underlying_of

log = logging.getLogger(__name__)

PriceListener = Callable[[str, float], None]


class SimMarket:
    """Shared price source for all simulated accounts.

    NQ and MNQ (and ES/MES, YM/MYM, RTY/M2K) share the same underlying price,
    so a leader NQ order and a follower MNQ order trigger at the same moment.
    """

    def __init__(
        self,
        prices: dict[str, float] | None = None,
        *,
        volatility_ticks: float = 1.5,
        tick_interval: float = 0.5,
        seed: int | None = None,
    ) -> None:
        self._prices: dict[str, float] = dict(DEFAULT_PRICES)
        for key, value in (prices or {}).items():
            self._prices[self._resolve(key)] = value
        self.volatility_ticks = volatility_ticks
        self.tick_interval = tick_interval
        self._rng = random.Random(seed)
        self._listeners: list[PriceListener] = []
        self._task: asyncio.Task | None = None
        self._walk_paused = False

    # ------------------------------------------------------------------ prices

    @staticmethod
    def _resolve(symbol_or_underlying: str) -> str:
        key = symbol_or_underlying.upper()
        if key in UNDERLYING_TICK:
            return key
        return underlying_of(key)

    def price(self, symbol_or_underlying: str) -> float:
        return self._prices[self._resolve(symbol_or_underlying)]

    def prices(self) -> dict[str, float]:
        return dict(self._prices)

    def set_price(self, symbol_or_underlying: str, price: float) -> float:
        underlying = self._resolve(symbol_or_underlying)
        if price <= 0:
            raise ValueError("Price must be positive")
        price = round_to_tick(price, UNDERLYING_TICK[underlying])
        self._prices[underlying] = price
        for listener in list(self._listeners):
            try:
                listener(underlying, price)
            except Exception:  # a broken listener must not stop the market
                log.exception("Price listener failed")
        return price

    def move(self, symbol_or_underlying: str, points: float) -> float:
        return self.set_price(symbol_or_underlying, self.price(symbol_or_underlying) + points)

    def add_listener(self, listener: PriceListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: PriceListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    # ------------------------------------------------------------------ random walk

    @property
    def walk_running(self) -> bool:
        return self._task is not None and not self._walk_paused

    def start(self) -> None:
        self._walk_paused = False
        if self._task is None:
            self._task = asyncio.create_task(self._walk(), name="sim-market-walk")

    def pause_walk(self) -> None:
        self._walk_paused = True

    def resume_walk(self) -> None:
        self._walk_paused = False

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _walk(self) -> None:
        while True:
            await asyncio.sleep(self.tick_interval)
            if self._walk_paused:
                continue
            for underlying, tick in UNDERLYING_TICK.items():
                steps = round(self._rng.gauss(0, self.volatility_ticks))
                if steps:
                    self.set_price(underlying, self._prices[underlying] + steps * tick)
