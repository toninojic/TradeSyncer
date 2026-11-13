"""Per-follower risk rules.

Three rules, all optional per follower:

* **Daily loss limit** – realized + unrealized P&L of the trading day. On a
  breach the follower is flattened and its copying is locked until the next
  trading day (or until it is re-enabled by hand, which gives it a fresh
  allowance of the same size).
* **Max position** – a hard cap per symbol. It caps the follower's target, so
  the follower simply holds less than the leader; the guard below additionally
  blocks any single order that would exceed it.
* **Trading hours** – outside the allowed window (New York time) the follower
  only reduces: no new entries, exits are still copied.

The copy engine talks to this module through :class:`RiskGuard`. The default
:class:`AllowAll` applies no rules (used by engine tests and the demo).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from app.brokers.base import OrderRequest
from app.core.trading_day import next_roll, trading_day, utcnow, within_trading_hours
from app.engine.events import (
    EngineRecord,
    EngineSink,
    LogCategory,
    LogLevel,
    LogRecord,
    MemorySink,
    RiskEventRecord,
    StatusChanged,
)

if TYPE_CHECKING:
    from app.engine.copy_engine import CopyEngine

log = logging.getLogger(__name__)


class Rule(StrEnum):
    DAILY_LOSS = "daily_loss_limit"
    MAX_POSITION = "max_position"
    TRADING_HOURS = "trading_hours"


# --------------------------------------------------------------------------- guard interface


class RiskGuard(Protocol):
    def check_order(
        self, follower_id: str, request: OrderRequest, current_position: int, reduces_exposure: bool
    ) -> str | None:
        """Return a reason string to block the order, or None to allow it."""
        ...

    def is_locked(self, follower_id: str) -> bool:
        """True while copying is disabled for this follower by a risk rule."""
        ...

    def outside_hours(self, follower_id: str) -> bool:
        """True if the follower has trading hours and it is outside them now."""
        ...

    def note(self, follower_id: str, rule: str, message: str) -> None:
        """Record that a rule limited the follower (deduplicated per trading day)."""
        ...


class AllowAll:
    """No rules."""

    def check_order(
        self, follower_id: str, request: OrderRequest, current_position: int, reduces_exposure: bool
    ) -> str | None:
        return None

    def is_locked(self, follower_id: str) -> bool:
        return False

    def outside_hours(self, follower_id: str) -> bool:
        return False

    def note(self, follower_id: str, rule: str, message: str) -> None:
        return None


# --------------------------------------------------------------------------- rules and state


@dataclass(slots=True)
class RiskRulesCfg:
    daily_loss_limit: float | None = None  # USD, positive
    max_position: int | None = None  # contracts per symbol
    trading_hours_enabled: bool = False
    trading_start: str = "09:30"  # New York time
    trading_end: str = "16:00"
    trading_days: tuple[int, ...] = (0, 1, 2, 3, 4)  # Monday = 0

    @property
    def active(self) -> bool:
        return (
            self.daily_loss_limit is not None
            or self.max_position is not None
            or self.trading_hours_enabled
        )


@dataclass(slots=True)
class RiskState:
    locked: bool = False
    locked_day: str | None = None
    locked_at: float | None = None
    reason: str | None = None
    # Day P&L at the last manual re-enable; the loss is measured from here.
    baseline: float = 0.0
    baseline_day: str | None = None


def usd(value: float) -> str:
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def loss_used(day_pnl: float, baseline: float) -> float:
    """How much of the allowance is used (never negative)."""
    return max(0.0, round(baseline - day_pnl, 2))


class RiskStore(Protocol):
    async def load(self) -> dict[str, RiskState]: ...

    async def save(self, follower_id: str, state: RiskState) -> None: ...


class MemoryRiskStore:
    def __init__(self) -> None:
        self.states: dict[str, RiskState] = {}

    async def load(self) -> dict[str, RiskState]:
        return dict(self.states)

    async def save(self, follower_id: str, state: RiskState) -> None:
        self.states[follower_id] = RiskState(
            state.locked, state.locked_day, state.locked_at, state.reason,
            state.baseline, state.baseline_day,
        )


# --------------------------------------------------------------------------- manager


class RiskManager:
    """Evaluates the rules, keeps the lock state and records triggered rules."""

    def __init__(
        self,
        *,
        sink: EngineSink | None = None,
        store: RiskStore | None = None,
        now_fn: Callable[[], datetime] = utcnow,
        check_interval_s: float = 1.0,
        min_interval_s: float = 0.05,
    ) -> None:
        self.sink: EngineSink = sink or MemorySink()
        self.store: RiskStore = store or MemoryRiskStore()
        self._now = now_fn
        self.check_interval_s = check_interval_s
        self.min_interval_s = min_interval_s
        self._rules: dict[str, RiskRulesCfg] = {}
        self._state: dict[str, RiskState] = {}
        self._last_pnl: dict[str, float] = {}
        self._counts: dict[tuple[str, str], int] = {}
        self._outbox: list[EngineRecord] = []
        self._engine: CopyEngine | None = None
        self._pnl: Callable[[str], float | None] | None = None
        self._on_new_day: Callable[[], None] | None = None
        self._day: str | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ wiring

    def attach(
        self,
        engine: "CopyEngine | None",
        pnl_provider: Callable[[str], float | None] | None,
        on_new_day: Callable[[], None] | None = None,
    ) -> None:
        self._engine = engine
        self._pnl = pnl_provider
        self._on_new_day = on_new_day

    def set_rules(self, rules: Mapping[str, RiskRulesCfg]) -> None:
        self._rules = dict(rules)

    def set_rule(self, follower_id: str, rules: RiskRulesCfg | None) -> None:
        if rules is None:
            self._rules.pop(follower_id, None)
        else:
            self._rules[follower_id] = rules
        self.poke()

    async def start(self) -> None:
        """Load persisted locks. A lock from an earlier trading day has expired."""
        self._day = self.today()
        for follower_id, state in (await self.store.load()).items():
            if state.locked and state.locked_day != self._day:
                state.locked, state.locked_day, state.locked_at, state.reason = False, None, None, None
                await self.store.save(follower_id, state)
            if state.baseline_day != self._day:
                state.baseline, state.baseline_day = 0.0, None
            self._state[follower_id] = state
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="risk-manager")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def poke(self) -> None:
        """Ask for an evaluation soon (price moved, rule changed, note queued)."""
        self._wake.set()

    # ------------------------------------------------------------------ RiskGuard

    def check_order(
        self, follower_id: str, request: OrderRequest, current_position: int, reduces_exposure: bool
    ) -> str | None:
        if reduces_exposure:
            return None  # getting out is always allowed
        if self.is_locked(follower_id):
            return "Daily loss limit reached: copying is disabled until the next trading day"
        rules = self._rules.get(follower_id)
        if rules is None:
            return None
        if self.outside_hours(follower_id):
            reason = (
                f"Outside allowed trading hours ({rules.trading_start}-{rules.trading_end} ET): "
                "exits only"
            )
            self.note(follower_id, Rule.TRADING_HOURS, reason)
            return reason
        if rules.max_position is not None:
            after = abs(current_position + request.side.sign * request.qty)
            if after > rules.max_position:
                reason = (
                    f"Max position {rules.max_position} would be exceeded "
                    f"({request.symbol}: {current_position:+d} {request.side} {request.qty})"
                )
                self.note(follower_id, Rule.MAX_POSITION, reason)
                return reason
        return None

    def is_locked(self, follower_id: str) -> bool:
        state = self._state.get(follower_id)
        return bool(state and state.locked)

    def outside_hours(self, follower_id: str) -> bool:
        rules = self._rules.get(follower_id)
        if rules is None or not rules.trading_hours_enabled:
            return False
        return not within_trading_hours(
            self._now(), rules.trading_start, rules.trading_end, rules.trading_days
        )

    def note(self, follower_id: str, rule: str, message: str) -> None:
        """Count the hit; write a risk event and a log line the first time each
        trading day (so the Risk page lists the rule once, not on every order)."""
        key = (follower_id, str(rule))
        self._counts[key] = self._counts.get(key, 0) + 1
        if self._counts[key] > 1:
            return
        self._outbox.append(RiskEventRecord(
            follower_id, str(rule), message, self.today(), self._now().timestamp()
        ))
        self._outbox.append(LogRecord(LogLevel.WARNING, LogCategory.RISK, message, follower_id))
        self.poke()

    # ------------------------------------------------------------------ evaluation

    def today(self) -> str:
        return trading_day(self._now()).isoformat()

    async def evaluate(self) -> None:
        """One pass: roll the trading day if needed, check loss limits, flush records."""
        async with self._lock:
            today = self.today()
            if self._day is None:
                self._day = today
            elif today != self._day:
                await self._new_day(today, simulated=False)
            for follower_id, rules in list(self._rules.items()):
                if rules.daily_loss_limit is None or self._pnl is None:
                    continue
                pnl = self._pnl(follower_id)
                if pnl is None:
                    continue
                self._last_pnl[follower_id] = pnl
                state = self._state_for(follower_id)
                if not state.locked and loss_used(pnl, state.baseline) >= rules.daily_loss_limit:
                    await self._breach(follower_id, rules.daily_loss_limit, pnl)
            await self._flush()

    async def _breach(self, follower_id: str, limit: float, pnl: float) -> None:
        state = self._state_for(follower_id)
        message = f"Daily loss limit {usd(limit)} reached (day P&L {usd(pnl)})"
        state.locked = True
        state.locked_day = self._day
        state.locked_at = self._now().timestamp()
        state.reason = message
        key = (follower_id, str(Rule.DAILY_LOSS))
        self._counts[key] = self._counts.get(key, 0) + 1
        await self.store.save(follower_id, state)
        self._outbox.append(RiskEventRecord(
            follower_id, str(Rule.DAILY_LOSS), message, self._day or self.today(), state.locked_at
        ))
        self._outbox.append(LogRecord(
            LogLevel.ERROR, LogCategory.RISK,
            f"{message}. Follower is flattened and copying is disabled until the next trading day.",
            follower_id,
        ))
        await self._flush()
        if self._engine is not None and follower_id in self._engine.workers:
            await self._engine.flatten_follower(follower_id, "Daily loss limit reached")
        self._outbox.append(StatusChanged(follower_id, "risk_locked"))

    async def unlock(self, follower_id: str) -> bool:
        """Manual re-enable. The follower gets a fresh allowance measured from
        its current day P&L. Returns False if it was not locked."""
        async with self._lock:
            state = self._state_for(follower_id)
            if not state.locked:
                return False
            pnl = self._pnl(follower_id) if self._pnl else None
            if pnl is None:
                pnl = self._last_pnl.get(follower_id, 0.0)
            state.locked, state.locked_day, state.locked_at, state.reason = False, None, None, None
            state.baseline = min(0.0, round(pnl, 2))
            state.baseline_day = self._day
            await self.store.save(follower_id, state)
            limit = (self._rules.get(follower_id) or RiskRulesCfg()).daily_loss_limit
            extra = f" Fresh loss allowance: {usd(limit)}." if limit else ""
            self._outbox.append(LogRecord(
                LogLevel.WARNING, LogCategory.RISK,
                f"Copying re-enabled manually after the daily loss limit.{extra}", follower_id,
            ))
            self._outbox.append(StatusChanged(follower_id, "risk_unlocked"))
            self._rejoin([follower_id])
            await self._flush()
            return True

    async def start_new_day(self) -> None:
        """Simulator helper: do what happens at the 17:00 Chicago roll, right now."""
        async with self._lock:
            await self._new_day(self._day or self.today(), simulated=True)
            await self._flush()

    async def _new_day(self, day: str, *, simulated: bool) -> None:
        self._day = day
        released = []
        for follower_id, state in self._state.items():
            changed = state.locked or state.baseline != 0.0
            if state.locked:
                released.append(follower_id)
                self._outbox.append(LogRecord(
                    LogLevel.INFO, LogCategory.RISK,
                    "New trading day: copying re-enabled after the daily loss limit", follower_id,
                ))
            state.locked, state.locked_day, state.locked_at, state.reason = False, None, None, None
            state.baseline, state.baseline_day = 0.0, None
            if changed:
                await self.store.save(follower_id, state)
        self._counts.clear()
        self._last_pnl.clear()
        if self._on_new_day is not None:
            try:
                self._on_new_day()
            except Exception:
                log.exception("on_new_day callback failed")
        suffix = " (started from the simulator)" if simulated else ""
        self._outbox.append(LogRecord(
            LogLevel.INFO, LogCategory.RISK,
            f"New trading day {day}{suffix}: daily P&L and risk counters reset",
        ))
        self._outbox.append(StatusChanged(None, "new_trading_day"))
        self._rejoin(released)

    def _rejoin(self, follower_ids: list[str]) -> None:
        if self._engine is None:
            return
        from app.engine.follower_worker import JoinAction

        for follower_id in follower_ids:
            worker = self._engine.workers.get(follower_id)
            if worker is not None:
                worker.submit(JoinAction())

    async def _flush(self) -> None:
        records, self._outbox = self._outbox, []
        for record in records:
            try:
                await self.sink.emit(record)
            except Exception:
                log.exception("Risk sink failed")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), self.check_interval_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.evaluate()
            except Exception:
                log.exception("Risk evaluation failed")
            await asyncio.sleep(self.min_interval_s)

    def _state_for(self, follower_id: str) -> RiskState:
        return self._state.setdefault(follower_id, RiskState())

    # ------------------------------------------------------------------ status for the UI

    def info(self) -> dict[str, Any]:
        now = self._now()
        return {"trading_day": self.today(), "next_reset": next_roll(now).timestamp()}

    def snapshot(self, follower_id: str) -> dict[str, Any] | None:
        rules = self._rules.get(follower_id)
        if rules is None:
            return None
        state = self._state.get(follower_id) or RiskState()
        pnl = self._last_pnl.get(follower_id)
        if self._pnl is not None:
            fresh = self._pnl(follower_id)
            pnl = fresh if fresh is not None else pnl
        return {
            "locked": state.locked,
            "locked_at": state.locked_at,
            "reason": state.reason,
            "daily_loss_limit": rules.daily_loss_limit,
            "loss_used": loss_used(pnl, state.baseline) if pnl is not None else None,
            "max_position": rules.max_position,
            "trading_hours_enabled": rules.trading_hours_enabled,
            "trading_start": rules.trading_start,
            "trading_end": rules.trading_end,
            "in_trading_hours": not self.outside_hours(follower_id),
            "triggered_today": {
                rule: count for (fid, rule), count in self._counts.items() if fid == follower_id
            },
        }
