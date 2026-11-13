"""Per-follower worker.

Each follower has its own queue and task, so a slow or disconnected follower
never delays the others. All state changes for one follower (leader events,
the follower's own broker events, reconciliation, flatten) run one at a time
on that queue, which removes race conditions inside a follower.

How a follower is kept in line with the leader
-----------------------------------------------
* The follower's *target* position is derived from the leader's position
  (see :mod:`app.engine.sizing`).
* Leader limit/stop entries are mirrored as resting orders ("linked" orders).
* Leader SL/TP orders are mirrored too, but never larger than the follower's
  actual position, so a protective stop can never reverse the follower.
* On every leader fill the follower is driven to its target with a market
  order for the *residual*: ``target - actual - pending``. ``pending`` counts
  working market orders and linked orders that are expected to fill because
  their leader counterpart already did.
* The reconciliation tick compares target and actual using a fresh broker
  snapshot and either auto-corrects or alerts.

Risk rules (see :mod:`app.engine.risk`)
---------------------------------------
* A follower locked by the daily loss limit does not copy at all.
* The max-position cap is part of the target, and resting entries plus the
  position never add up to more than the cap ("room").
* Outside the allowed trading hours the follower is *reduce-only*: its target
  is clamped so it can only shrink its position. When the window opens while
  the leader is in a trade, the follower stays reduce-only until the leader is
  flat (no joining mid-trade).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.brokers.base import (
    BrokerAdapter,
    BrokerError,
    BrokerEvent,
    BrokerEventType,
    ConnectionStatus,
    NotConnectedError,
    Order,
    OrderNotWorkingError,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    new_id,
)
from app.engine.config import FollowerConfig, MismatchMode
from app.engine.connection import ConnectionSupervisor
from app.engine.events import (
    FollowerAction,
    FollowerOrderRecord,
    LogCategory,
    LogLevel,
    RecordStatus,
    StatusChanged,
)
from app.engine.reconciler import ReconcileAction, SyncStatus, decide
from app.engine.sizing import follower_targets, order_delta, reduce_only_target, reduces_exposure
from app.engine.state import PositionBook

if TYPE_CHECKING:
    from app.engine.copy_engine import CopyEngine

log = logging.getLogger(__name__)


class SyncState(StrEnum):
    SYNCED = "synced"  # copying normally
    WAIT_FOR_FLAT = "wait_for_flat"  # do not join an open leader trade; wait until the leader is flat


@dataclass(slots=True)
class OrderLink:
    """Leader order <-> follower order mirror."""

    leader_order_id: str
    kind: str  # "entry" | "exit"
    follower_order_id: str | None = None
    status: str = "pending"  # pending | active | skipped | failed | closed
    leader_filled_mono: float | None = None
    follower_filled_mono: float | None = None
    placements: int = 0


@dataclass(slots=True)
class OrderMeta:
    action: FollowerAction
    record: FollowerOrderRecord
    leader_order_id: str | None
    sent_mono: float


# ----------------------------------------------------------------------- queue actions


@dataclass(slots=True)
class LeaderEventAction:
    event: BrokerEvent
    received_mono: float
    leader_positions: dict[str, int]
    leader_flat: bool
    fill_seq: int = 0


@dataclass(slots=True)
class FollowerEventAction:
    event: BrokerEvent


@dataclass(slots=True)
class ReconcileTick:
    pass


@dataclass(slots=True)
class ResyncAction:
    initial: bool = False


@dataclass(slots=True)
class JoinAction:
    """Re-evaluate whether to copy right away or wait for the leader to be flat."""


@dataclass(slots=True)
class CallAction:
    fn: Callable[[], Awaitable[None]]


MAX_EXIT_PLACEMENTS = 3


class FollowerWorker:
    def __init__(self, engine: "CopyEngine", adapter: BrokerAdapter, cfg: FollowerConfig) -> None:
        self.engine = engine
        self.adapter = adapter
        self.cfg = cfg
        self.id = adapter.account_id
        self.book = PositionBook()
        self.meta: dict[str, OrderMeta] = {}
        self.links: dict[str, OrderLink] = {}
        self.links_by_follower_order: dict[str, OrderLink] = {}
        self.sync_state = SyncState.WAIT_FOR_FLAT
        self.sync_status = SyncStatus.DISCONNECTED
        self.symbol_status: dict[str, str] = {}
        self.correction_attempts = 0
        self.alerted: set[str] = set()
        self.latencies: deque[float] = deque(maxlen=engine.settings.latency_samples)
        self.halted = False
        self.flatten_pending = False  # a flatten failed (e.g. offline); retried on reconnect
        self.reduce_only = False  # outside trading hours: exits only (see _update_restrictions)
        self.initialized = False
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self._busy = False
        self._reconcile_queued = False
        self._stream = None
        self._tasks: list[asyncio.Task] = []
        self.supervisor = ConnectionSupervisor(
            adapter, on_connected=self._on_connected, on_attempt_failed=self._on_connect_failed
        )

    # ================================================================== lifecycle

    async def start(self) -> None:
        self._stream = self.adapter.subscribe()
        self._tasks = [
            asyncio.create_task(self._pump(), name=f"pump-{self.id}"),
            asyncio.create_task(self._run(), name=f"worker-{self.id}"),
        ]
        self.supervisor.restart()
        self.supervisor.ensure_connected()

    async def stop(self, disconnect: bool = True) -> None:
        await self.supervisor.stop()
        if self._stream:
            self._stream.close()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        if disconnect and self.adapter.is_connected:
            try:
                await self.adapter.disconnect()
            except BrokerError:
                pass

    def submit(self, action: Any) -> None:
        if isinstance(action, ReconcileTick):
            if self._reconcile_queued:
                return
            self._reconcile_queued = True
        self.queue.put_nowait(action)

    def is_busy(self) -> bool:
        pending_stream = self._stream.pending if self._stream else 0
        return self._busy or not self.queue.empty() or pending_stream > 0

    def retry_sync(self) -> None:
        """Reset the auto-correct attempt counter and reconcile now."""
        self.correction_attempts = 0
        self.alerted.clear()
        self.submit(ReconcileTick())

    def update_config(self, cfg: FollowerConfig) -> None:
        was_enabled = self.cfg.copy_enabled
        self.cfg = cfg
        self.correction_attempts = 0
        self.alerted.clear()
        if cfg.copy_enabled and not was_enabled:
            self.submit(JoinAction())

    # ================================================================== loops

    async def _pump(self) -> None:
        assert self._stream is not None
        async for event in self._stream:
            if event.type is BrokerEventType.CONNECTION_STATUS:
                await self._on_connection_event(event)
            else:
                self.queue.put_nowait(FollowerEventAction(event))

    async def _run(self) -> None:
        while True:
            action = await self.queue.get()
            self._busy = True
            try:
                await self._dispatch(action)
            except Exception as exc:  # never let one bad action kill the worker
                log.exception("Follower worker %s failed on %r", self.id, action)
                await self._log(LogLevel.ERROR, LogCategory.ENGINE, f"Internal error: {exc!r}")
            finally:
                self._busy = False
                self.queue.task_done()

    async def _dispatch(self, action: Any) -> None:
        if isinstance(action, LeaderEventAction):
            await self._handle_leader_event(action)
        elif isinstance(action, FollowerEventAction):
            await self._handle_follower_event(action.event)
        elif isinstance(action, ReconcileTick):
            self._reconcile_queued = False
            await self._reconcile()
        elif isinstance(action, ResyncAction):
            await self._resync(initial=action.initial)
            await self._reconcile()
        elif isinstance(action, JoinAction):
            self._evaluate_join()
            await self._reconcile()
        elif isinstance(action, CallAction):
            await action.fn()

    # ================================================================== connection

    async def _on_connection_event(self, event: BrokerEvent) -> None:
        status = event.connection
        if status in (ConnectionStatus.DISCONNECTED, ConnectionStatus.ERROR):
            if self.sync_status is not SyncStatus.DISCONNECTED:
                await self._log(
                    LogLevel.WARNING, LogCategory.CONNECTION,
                    f"Follower disconnected: {event.message or status}",
                )
            await self._set_status(SyncStatus.DISCONNECTED)
            self.supervisor.ensure_connected()

    async def _on_connected(self) -> None:
        await self._log(LogLevel.INFO, LogCategory.CONNECTION, "Follower connected")
        self.queue.put_nowait(ResyncAction(initial=not self.initialized))

    async def _on_connect_failed(self, exc: Exception, delay: float, attempt: int) -> None:
        await self._log(
            LogLevel.WARNING, LogCategory.CONNECTION,
            f"Reconnect attempt {attempt} failed ({exc}); retrying in {delay:.1f}s",
        )

    # ================================================================== leader events

    async def _handle_leader_event(self, action: LeaderEventAction) -> None:
        event = action.event
        relevant = event.order is not None and (
            event.type is BrokerEventType.ORDER_FILLED
            or (event.type is BrokerEventType.ORDER_PLACED and event.order.type is not OrderType.MARKET)
        )
        if (relevant and self.initialized and self.engine.running and not self.halted
                and self.cfg.copy_enabled and not self.adapter.is_connected
                and not self.engine.risk.is_locked(self.id)):
            # Leave a trace in the trade log; reconciliation catches up after reconnect.
            symbol = self.cfg.mapping_for(event.order.symbol).follower_symbol
            await self._emit(FollowerOrderRecord(
                id=new_id("fo-"), follower_id=self.id,
                action=FollowerAction.COPY_FILL if event.type is BrokerEventType.ORDER_FILLED
                else FollowerAction.MIRROR_ENTRY,
                status=RecordStatus.FAILED, leader_event_id=event.event_id,
                leader_order_id=event.order.id, symbol=symbol,
                error="Follower not connected; will reconcile after reconnect",
            ))
            self.supervisor.ensure_connected()
            return
        if not self._copying_allowed():
            return
        if self.sync_state is SyncState.WAIT_FOR_FLAT:
            if action.leader_flat:
                self.sync_state = SyncState.SYNCED
                await self._log(
                    LogLevel.INFO, LogCategory.ENGINE,
                    "Leader is flat: follower joins and copies from the next trade",
                )
                await self._emit(StatusChanged(self.id, "joined"))
            return
        await self._update_restrictions()
        if event.type is BrokerEventType.ORDER_PLACED:
            await self._on_leader_order_placed(action)
        elif event.type is BrokerEventType.ORDER_MODIFIED:
            await self._on_leader_order_modified(action)
        elif event.type in (BrokerEventType.ORDER_CANCELLED, BrokerEventType.ORDER_REJECTED):
            await self._on_leader_order_done(action)
        elif event.type is BrokerEventType.ORDER_FILLED:
            await self._on_leader_fill(action)

    def _copying_allowed(self) -> bool:
        return (
            self.initialized
            and self.engine.running
            and not self.halted
            and self.cfg.copy_enabled
            and self.adapter.is_connected
            and not self.engine.risk.is_locked(self.id)
        )

    async def _update_restrictions(self) -> None:
        """Enter reduce-only mode outside the trading hours; leave it when the
        window is open again and the follower would not be joining mid-trade."""
        if self.engine.risk.outside_hours(self.id):
            if not self.reduce_only:
                self.reduce_only = True
                await self._log(LogLevel.INFO, LogCategory.RISK,
                                "Outside allowed trading hours: exits only, no new entries")
                await self._emit(StatusChanged(self.id, "reduce_only"))
            return
        if not self.reduce_only:
            return
        raw = follower_targets(self.engine.leader.positions, self.cfg)
        in_line = all(self.book.position(symbol) == target for symbol, target in raw.items())
        if self.engine.leader.is_flat() or in_line:
            self.reduce_only = False
            await self._log(LogLevel.INFO, LogCategory.RISK,
                            "Trading hours are open: copying entries again")
            await self._emit(StatusChanged(self.id, "reduce_only_off"))

    def _leader_order(self, order: Order) -> Order:
        return self.engine.leader.orders.get(order.id, order)

    async def _on_leader_order_placed(self, action: LeaderEventAction) -> None:
        event = action.event
        assert event.order is not None
        lo = self._leader_order(event.order)
        kind = self.engine.leader.classify(lo)
        if kind == "market" or lo.id in self.links:
            return
        if kind == "exit":
            link = OrderLink(lo.id, "exit")
            self.links[lo.id] = link
            if not self.cfg.copy_sl_tp:
                link.status = "skipped"
                await self._record_skip(event, lo, "Copy SL/TP is off for this follower")
                return
            await self._sync_exits(event.event_id, action.received_mono)
            return

        link = OrderLink(lo.id, "entry")
        self.links[lo.id] = link
        if not lo.is_active:
            link.status = "skipped"
            return
        await self._place_entry_mirror(link, lo, event.event_id, action.received_mono)

    async def _place_entry_mirror(
        self, link: OrderLink, lo: Order, leader_event_id: str, received: float | None
    ) -> None:
        symbol, delta = order_delta(
            self.engine.leader.positions, self.cfg, lo.symbol, lo.side.sign * lo.remaining
        )
        if delta == 0:
            link.status = "skipped"
            await self._record_skip_raw(leader_event_id, lo.id, symbol, self._zero_size_reason(lo.symbol))
            return
        if self.reduce_only:
            link.status = "skipped"
            reason = "Outside allowed trading hours: entry order not copied"
            self.engine.risk.note(self.id, "trading_hours", reason)
            await self._record_skip_raw(leader_event_id, lo.id, symbol, reason)
            return
        side = Side.from_sign(delta)
        qty = abs(delta)
        room = self._entry_room(symbol, side)
        if room is not None and qty > room:
            reason = (f"Max position {self.cfg.risk_max_position}: entry order "
                      f"{'not copied' if room == 0 else f'reduced from {qty} to {room}'}")
            self.engine.risk.note(self.id, "max_position", f"{symbol}: {reason}")
            if room == 0:
                link.status = "skipped"
                await self._record_skip_raw(leader_event_id, lo.id, symbol, reason)
                return
            qty = room
        request = OrderRequest(
            symbol=symbol, side=side, qty=qty, type=lo.type,
            limit_price=lo.limit_price, stop_price=lo.stop_price,
            client_order_id=f"{leader_event_id}:{self.id}:{link.placements}",
        )
        link.placements += 1
        placed = await self._send(
            request, FollowerAction.MIRROR_ENTRY, leader_event_id=leader_event_id,
            leader_order_id=lo.id, received=received,
        )
        self._attach(link, placed)

    def _attach(self, link: OrderLink, placed: Order | None) -> None:
        if placed is not None and placed.status is not OrderStatus.REJECTED:
            link.follower_order_id = placed.id
            link.status = "active"
            self.links_by_follower_order[placed.id] = link
            if placed.filled_qty > 0 and link.follower_filled_mono is None:
                link.follower_filled_mono = time.monotonic()
        else:
            link.status = "failed"

    async def _on_leader_order_modified(self, action: LeaderEventAction) -> None:
        event = action.event
        assert event.order is not None
        lo = self._leader_order(event.order)
        kind = self.engine.leader.classify(lo)
        if kind == "exit":
            await self._sync_exits(event.event_id, action.received_mono)
            return
        link = self.links.get(lo.id)
        if kind != "entry" or link is None or not lo.is_active:
            return
        fo = self._linked_order(link)
        symbol, delta = order_delta(
            self.engine.leader.positions, self.cfg, lo.symbol, lo.side.sign * lo.remaining
        )
        if fo is None:
            if link.status in ("skipped", "failed") and delta != 0 and lo.filled_qty == 0:
                await self._place_entry_mirror(link, lo, event.event_id, action.received_mono)
            return
        if delta == 0:
            await self._cancel(fo, "Leader order resized below 1 follower contract",
                               leader_event_id=event.event_id, leader_order_id=lo.id)
            return
        changes: dict[str, Any] = {}
        wanted = abs(delta)
        room = self._entry_room(symbol, fo.side, exclude_order_id=fo.id)
        if room is not None and wanted > room:
            self.engine.risk.note(
                self.id, "max_position",
                f"{symbol}: Max position {self.cfg.risk_max_position}: entry order limited to {room}",
            )
            wanted = room
        if wanted == 0:
            await self._cancel(fo, f"Max position {self.cfg.risk_max_position} reached",
                               leader_event_id=event.event_id, leader_order_id=lo.id)
            return
        new_qty = fo.filled_qty + wanted
        if new_qty != fo.qty:
            changes["qty"] = new_qty
        if lo.type is OrderType.LIMIT and lo.limit_price != fo.limit_price:
            changes["limit_price"] = lo.limit_price
        if lo.type is OrderType.STOP and lo.stop_price != fo.stop_price:
            changes["stop_price"] = lo.stop_price
        if changes:
            await self._modify(fo, changes, leader_event_id=event.event_id,
                               leader_order_id=lo.id, received=action.received_mono)

    async def _on_leader_order_done(self, action: LeaderEventAction) -> None:
        event = action.event
        assert event.order is not None
        lo = self._leader_order(event.order)
        link = self.links.get(lo.id)
        if link is None:
            return
        fo = self._linked_order(link)
        if fo is not None:
            await self._cancel(fo, "Leader order cancelled", leader_event_id=event.event_id,
                               leader_order_id=lo.id, received=action.received_mono)
        link.status = "closed"
        if link.kind == "exit":
            await self._sync_exits(event.event_id, action.received_mono)

    async def _on_leader_fill(self, action: LeaderEventAction) -> None:
        event = action.event
        assert event.order is not None and event.fill is not None
        lo = self._leader_order(event.order)
        link = self.links.get(lo.id)
        if link is not None and link.leader_filled_mono is None:
            link.leader_filled_mono = time.monotonic()
        # Mirrored orders of *other* leader orders that should have filled already
        # are cancelled first, so the residual below is computed on real positions
        # (this is the "leader closes while follower order is pending" case).
        await self._cancel_lagging(exclude=lo.id, expired_only=False, leader_event_id=event.event_id)

        covered = link is not None and self._linked_order(link) is not None
        if self.cfg.mismatch_mode is MismatchMode.AUTO_CORRECT:
            if action.fill_seq != self.engine.leader_fill_seq:
                # A newer leader fill is already known and queued behind this one.
                # Positions and orders are always evaluated at their *latest* state,
                # so acting now would trade a stale intermediate state (churn).
                await self._record_skip_raw(
                    event.event_id, lo.id, self.cfg.mapping_for(lo.symbol).follower_symbol,
                    "Superseded by a newer leader fill",
                )
                return
            sent = await self._drive_to_target(
                FollowerAction.COPY_FILL, leader_event_id=event.event_id,
                leader_order_id=lo.id, received=action.received_mono,
            )
        else:
            # Alert-only followers copy each fill's own size change and are never
            # "corrected" towards the target.
            sent = 0
            if not covered:
                fill = event.fill
                after = dict(action.leader_positions)
                before = dict(after)
                before[fill.symbol] = before.get(fill.symbol, 0) - fill.side.sign * fill.qty
                symbol, delta = order_delta(before, self.cfg, fill.symbol, fill.side.sign * fill.qty)
                if self.reduce_only:
                    delta = self._reduce_only_delta(symbol, delta)
                if delta:
                    await self._send(
                        OrderRequest(symbol, Side.from_sign(delta), abs(delta),
                                     client_order_id=f"{event.event_id}:{self.id}:{symbol}"),
                        FollowerAction.COPY_FILL, leader_event_id=event.event_id,
                        leader_order_id=lo.id, received=action.received_mono,
                    )
                    sent = 1
        if sent == 0 and not covered:
            symbol = self.cfg.mapping_for(lo.symbol).follower_symbol
            await self._record_skip_raw(event.event_id, lo.id, symbol,
                                        self._zero_size_reason(lo.symbol))
        self._note_cap(lo.symbol)
        await self._sync_exits(event.event_id, action.received_mono)

    # ================================================================== follower events

    async def _handle_follower_event(self, event: BrokerEvent) -> None:
        if event.order is None:
            if event.type is BrokerEventType.POSITION_CHANGED:
                await self._emit(StatusChanged(self.id, "position"))
            return
        delta = self.book.apply_order(event.order)
        order = self.book.orders[event.order.id]
        meta = self.meta.get(order.id)
        if meta is not None and self._update_record(meta.record, order):
            await self._emit(meta.record)
        link = self.links_by_follower_order.get(order.id)
        if link is not None and order.filled_qty > 0 and link.follower_filled_mono is None:
            link.follower_filled_mono = time.monotonic()
        if delta:
            await self._emit(StatusChanged(self.id, "position"))
            if self._copying_allowed() and self.sync_state is SyncState.SYNCED:
                await self._sync_exits()

    # ================================================================== core: targets & pending

    def targets(self) -> dict[str, int]:
        targets = follower_targets(self.engine.leader.positions, self.cfg)
        if self.reduce_only:
            for symbol, target in targets.items():
                targets[symbol] = reduce_only_target(target, self.book.position(symbol))
        return targets

    # ------------------------------------------------------------------ risk helpers

    def _zero_size_reason(self, leader_symbol: str) -> str:
        """Why nothing was sent for a leader event (shown in the trade log)."""
        symbol = self.cfg.mapping_for(leader_symbol).follower_symbol
        raw = follower_targets(self.engine.leader.positions, self.cfg).get(symbol, 0)
        if self.reduce_only and raw != self.targets().get(symbol, 0):
            reason = "Outside allowed trading hours: entry not copied (exits only)"
            self.engine.risk.note(self.id, "trading_hours", reason)
            return reason
        if self._risk_cap_binding(symbol):
            return f"Max position {self.cfg.risk_max_position} reached"
        return "Follower already at target (size rounds to 0)"

    def _risk_cap_binding(self, symbol: str) -> bool:
        cap = self.cfg.risk_max_position
        if cap is None or (self.cfg.max_contracts is not None and self.cfg.max_contracts <= cap):
            return False
        wanted = follower_targets(self.engine.leader.positions, self.cfg, capped=False).get(symbol, 0)
        return abs(wanted) > cap

    def _note_cap(self, leader_symbol: str) -> None:
        symbol = self.cfg.mapping_for(leader_symbol).follower_symbol
        if self._risk_cap_binding(symbol):
            wanted = follower_targets(self.engine.leader.positions, self.cfg, capped=False)[symbol]
            self.engine.risk.note(
                self.id, "max_position",
                f"{symbol}: position capped at {self.cfg.risk_max_position} "
                f"(leader size would give {abs(wanted)})",
            )

    def _reduce_only_delta(self, symbol: str, delta: int) -> int:
        """Limit a signed order quantity so it can only shrink the position."""
        position = self.book.position(symbol) + self._pending(symbol)
        if delta == 0 or position == 0 or (position > 0) == (delta > 0):
            return 0
        return -position if abs(delta) > abs(position) else delta

    def _resting_entries(self, symbol: str, side: Side) -> list[Order]:
        """Working mirrored entry orders on one side (they add exposure when they fill)."""
        result = []
        for order in self.book.active_orders():
            link = self.links_by_follower_order.get(order.id)
            if (order.symbol == symbol and order.side is side and order.type is not OrderType.MARKET
                    and link is not None and link.kind == "entry"):
                result.append(order)
        return result

    def _entry_room(self, symbol: str, side: Side, exclude_order_id: str | None = None) -> int | None:
        """Contracts that can still be added on `side` without the position plus
        all resting entries exceeding the risk cap. None = no cap."""
        cap = self.cfg.risk_max_position
        if cap is None:
            return None
        exposure = side.sign * (self.book.position(symbol) + self._pending(symbol))
        resting = sum(o.remaining for o in self._resting_entries(symbol, side)
                      if o.id != exclude_order_id)
        return max(0, cap - exposure - resting)

    async def _make_room(self, request: OrderRequest) -> None:
        """A market order needs room under the cap: cancel resting entries on the
        same side (newest first) until it fits."""
        room = self._entry_room(request.symbol, request.side)
        if room is None or request.qty <= room:
            return
        for order in sorted(self._resting_entries(request.symbol, request.side),
                            key=lambda o: o.created_at, reverse=True):
            await self._cancel(order, f"Max position {self.cfg.risk_max_position}: "
                                      "making room for a market order")
            room = self._entry_room(request.symbol, request.side)
            if room is None or request.qty <= room:
                return

    def _pending(self, symbol: str) -> int:
        total = 0
        for order_id in self.meta:
            order = self.book.orders.get(order_id)
            if order and order.is_active and order.type is OrderType.MARKET and order.symbol == symbol:
                total += order.side.sign * order.remaining
        for link in self.links.values():
            fo = self.book.orders.get(link.follower_order_id) if link.follower_order_id else None
            if fo is not None and fo.symbol == symbol:
                total += self._link_adjustment(link, fo)
        return total

    def _link_adjustment(self, link: OrderLink, fo: Order) -> int:
        """Signed quantity the follower is expected to move because of a mirrored
        order whose leader counterpart filled more (or less) than it did."""
        lo = self.engine.leader.orders.get(link.leader_order_id)
        if lo is None:
            return 0
        if lo.status is OrderStatus.FILLED:
            expected = fo.qty
        elif lo.qty > 0:
            expected = fo.qty * lo.filled_qty // lo.qty
        else:
            expected = 0
        if not fo.is_active:
            expected = min(expected, fo.filled_qty)
        diff = expected - fo.filled_qty
        if diff < 0:
            if not lo.is_active:
                diff = 0
            elif link.kind == "exit":
                grace = self.engine.settings.linked_fill_grace_s
                filled_at = link.follower_filled_mono
                if filled_at is not None and time.monotonic() - filled_at > grace:
                    diff = 0
        return diff * fo.side.sign

    def _linked_order(self, link: OrderLink) -> Order | None:
        if not link.follower_order_id:
            return None
        order = self.book.orders.get(link.follower_order_id)
        return order if order is not None and order.is_active else None

    async def _drive_to_target(
        self,
        action: FollowerAction,
        *,
        leader_event_id: str | None = None,
        leader_order_id: str | None = None,
        received: float | None = None,
    ) -> int:
        sent = 0
        for symbol, target in sorted(self.targets().items()):
            gap = target - self.book.position(symbol) - self._pending(symbol)
            if gap == 0:
                continue
            key = leader_event_id or new_id()
            request = OrderRequest(
                symbol, Side.from_sign(gap), abs(gap), client_order_id=f"{key}:{self.id}:{symbol}"
            )
            await self._send(request, action, leader_event_id=leader_event_id,
                             leader_order_id=leader_order_id, received=received)
            sent += 1
        return sent

    # ================================================================== protective orders (SL/TP)

    async def _sync_exits(self, leader_event_id: str | None = None, received: float | None = None) -> None:
        """Make the follower's mirrored SL/TP orders match the leader's, never
        exceeding the follower's actual position."""
        if not self.cfg.copy_sl_tp or not self._copying_allowed() or self.sync_state is not SyncState.SYNCED:
            return
        leader = self.engine.leader
        grace = self.engine.settings.linked_fill_grace_s
        now = time.monotonic()
        for link in list(self.links.values()):
            if link.kind != "exit" or link.status in ("skipped", "closed"):
                continue
            lo = leader.orders.get(link.leader_order_id)
            fo = self._linked_order(link)
            if lo is None or not lo.is_active:
                if fo is not None:
                    if lo is not None and lo.filled_qty > 0 and link.leader_filled_mono is None:
                        link.leader_filled_mono = now
                    lagging = (
                        lo is not None and lo.filled_qty > 0 and link.leader_filled_mono is not None
                        and now - link.leader_filled_mono < grace
                    )
                    if not lagging:
                        await self._cancel(fo, "Leader exit order is no longer working",
                                           leader_event_id=leader_event_id, leader_order_id=link.leader_order_id)
                else:
                    link.status = "closed"
                continue

            symbol, delta = order_delta(leader.positions, self.cfg, lo.symbol, lo.side.sign * lo.remaining)
            position = self.book.position(symbol)
            allowed = abs(position) if position * lo.side.sign < 0 else 0
            desired = min(abs(delta), allowed)

            if fo is None:
                if desired == 0 or link.placements >= MAX_EXIT_PLACEMENTS:
                    continue
                adopted = self._find_adoptable(symbol, lo)
                if adopted is not None:
                    # e.g. after a restart: the follower already has this SL/TP
                    self._attach(link, adopted)
                    continue
                request = OrderRequest(
                    symbol=symbol, side=lo.side, qty=desired, type=lo.type,
                    limit_price=lo.limit_price, stop_price=lo.stop_price,
                    oco_group=f"{self.id}:{lo.oco_group}" if lo.oco_group else None,
                    client_order_id=f"{lo.id}:{self.id}:{link.placements}",
                )
                link.placements += 1
                placed = await self._send(request, FollowerAction.MIRROR_EXIT,
                                          leader_event_id=leader_event_id, leader_order_id=lo.id,
                                          received=received)
                self._attach(link, placed)
                if link.status == "failed" and link.placements < MAX_EXIT_PLACEMENTS:
                    link.status = "pending"
                continue

            if desired == 0:
                await self._cancel(fo, "Follower has no position for this exit order",
                                   leader_event_id=leader_event_id, leader_order_id=lo.id)
                continue
            changes: dict[str, Any] = {}
            if fo.remaining != desired:
                changes["qty"] = fo.filled_qty + desired
            if lo.type is OrderType.LIMIT and fo.limit_price != lo.limit_price:
                changes["limit_price"] = lo.limit_price
            if lo.type is OrderType.STOP and fo.stop_price != lo.stop_price:
                changes["stop_price"] = lo.stop_price
            if changes:
                await self._modify(fo, changes, leader_event_id=leader_event_id,
                                   leader_order_id=lo.id, received=received)

    def _find_adoptable(self, symbol: str, lo: Order) -> Order | None:
        for order in self.book.active_orders():
            if (order.id not in self.links_by_follower_order and order.id not in self.meta
                    and order.symbol == symbol and order.side is lo.side and order.type is lo.type
                    and order.price == lo.price):
                return order
        return None

    def _link_leader_exits(self) -> None:
        """Create links for leader SL/TP orders that were already working when
        this follower started copying (startup, resume)."""
        for lo in self.engine.leader.active_orders():
            if lo.id not in self.links and self.engine.leader.classify(lo) == "exit":
                self.links[lo.id] = OrderLink(lo.id, "exit")

    async def _cancel_lagging(
        self, *, exclude: str | None, expired_only: bool, leader_event_id: str | None = None
    ) -> None:
        grace = self.engine.settings.linked_fill_grace_s
        now = time.monotonic()
        for link in list(self.links.values()):
            if link.leader_order_id == exclude:
                continue
            fo = self._linked_order(link)
            if fo is None:
                continue
            lo = self.engine.leader.orders.get(link.leader_order_id)
            if lo is not None and lo.is_active:
                continue  # the leader order is still working, so ours legitimately is too
            if lo is not None and lo.filled_qty > 0:
                if link.leader_filled_mono is None:
                    link.leader_filled_mono = now
                if expired_only and now - link.leader_filled_mono < grace:
                    continue
                reason = "Mirrored order did not fill together with the leader"
            else:
                reason = "Leader order is gone"
            await self._cancel(fo, reason, leader_event_id=leader_event_id,
                               leader_order_id=link.leader_order_id)

    async def _cancel_stale_market_orders(self) -> None:
        limit = self.engine.settings.stale_market_order_s
        now = time.monotonic()
        for order_id, meta in list(self.meta.items()):
            order = self.book.orders.get(order_id)
            if (order is not None and order.is_active and order.type is OrderType.MARKET
                    and now - meta.sent_mono > limit):
                await self._cancel(order, "Market order still working; cancelling the remainder")

    # ================================================================== reconciliation

    async def _reconcile(self) -> None:
        if not self.adapter.is_connected:
            await self._set_status(SyncStatus.DISCONNECTED)
            self.supervisor.ensure_connected()
            return
        await self._resync(initial=not self.initialized)
        if not self.initialized:
            return
        if self.flatten_pending and self.adapter.is_connected:
            await self._flatten_now("Retrying a flatten that failed while offline")
        if not self.cfg.copy_enabled:
            await self._set_status(SyncStatus.DISABLED)
            return
        if self.engine.risk.is_locked(self.id):
            await self._set_status(SyncStatus.RISK_LOCKED)
            return
        if not self.engine.running or self.halted:
            await self._set_status(SyncStatus.PAUSED)
            return
        if self.sync_state is SyncState.WAIT_FOR_FLAT:
            if self.engine.leader_initialized and self.engine.leader.is_flat():
                self.sync_state = SyncState.SYNCED
                await self._log(LogLevel.INFO, LogCategory.ENGINE,
                                "Leader is flat: follower joins and copies from the next trade")
            else:
                await self._set_status(SyncStatus.WAITING)
                return

        await self._update_restrictions()
        await self._cancel_lagging(exclude=None, expired_only=True)
        await self._cancel_stale_market_orders()

        statuses: dict[str, str] = {}
        worst = SyncStatus.IN_SYNC
        for symbol, target in sorted(self.targets().items()):
            actual = self.book.position(symbol)
            pending = self._pending(symbol)
            decision = decide(
                target=target, actual=actual, pending=pending, mode=self.cfg.mismatch_mode,
                attempts=self.correction_attempts,
                max_attempts=self.engine.settings.max_correction_attempts,
            )
            if decision.status is not SyncStatus.IN_SYNC or target or actual:
                statuses[symbol] = decision.status.value
            if decision.status is SyncStatus.OUT_OF_SYNC:
                worst = SyncStatus.OUT_OF_SYNC
            elif decision.status is SyncStatus.PENDING and worst is SyncStatus.IN_SYNC:
                worst = SyncStatus.PENDING

            if decision.action is ReconcileAction.CORRECT:
                self.correction_attempts += 1
                gap = decision.correction
                await self._log(
                    LogLevel.WARNING, LogCategory.RECONCILE,
                    f"{symbol}: expected {target}, actual {actual}. Auto-correcting with "
                    f"{Side.from_sign(gap)} {abs(gap)} (attempt {self.correction_attempts})",
                    data={"symbol": symbol, "target": target, "actual": actual, "pending": pending},
                )
                await self._send(
                    OrderRequest(symbol, Side.from_sign(gap), abs(gap),
                                 client_order_id=f"rec-{new_id()}:{self.id}"),
                    FollowerAction.RECONCILE,
                )
            elif decision.action is ReconcileAction.ALERT and symbol not in self.alerted:
                self.alerted.add(symbol)
                await self._log(
                    LogLevel.ERROR, LogCategory.RECONCILE,
                    f"{symbol}: out of sync (expected {target}, actual {actual}); {decision.reason}",
                    data={"symbol": symbol, "target": target, "actual": actual, "pending": pending},
                )

        self.symbol_status = statuses
        if worst is SyncStatus.IN_SYNC:
            if self.correction_attempts or self.alerted:
                await self._log(LogLevel.INFO, LogCategory.RECONCILE, "Back in sync")
            self.correction_attempts = 0
            self.alerted.clear()
        await self._set_status(worst)
        await self._sync_exits()

    async def _resync(self, *, initial: bool) -> None:
        try:
            orders = await self.adapter.get_orders(active_only=False)
            positions = await self.adapter.get_positions()
        except NotConnectedError:
            self.supervisor.ensure_connected()
            return
        except BrokerError as exc:
            await self._log(LogLevel.WARNING, LogCategory.RECONCILE, f"Snapshot failed: {exc}")
            return
        if initial or not self.initialized:
            self.book.load_initial(orders, positions)
            self.initialized = True
            self._evaluate_join()
        else:
            self.book.apply_snapshot_orders(orders)
            for symbol, local, broker in self.book.verify_positions(positions):
                await self._log(
                    LogLevel.WARNING, LogCategory.RECONCILE,
                    f"{symbol}: local position {local} differed from broker {broker}; using broker value",
                )
        for order in orders:
            meta = self.meta.get(order.id)
            if meta is not None and self._update_record(meta.record, self.book.orders[order.id]):
                await self._emit(meta.record)

    def _evaluate_join(self) -> None:
        if not self.engine.leader_initialized or not self.initialized:
            return
        targets = self.targets()
        in_line = all(self.book.position(s) == t for s, t in targets.items())
        if self.engine.leader.is_flat() or in_line:
            self.sync_state = SyncState.SYNCED
            self._link_leader_exits()
        else:
            self.sync_state = SyncState.WAIT_FOR_FLAT

    # ================================================================== flatten / kill

    async def emergency_flatten(self, reason: str, *, keep_halted: bool) -> None:
        """Flatten immediately, without waiting for queued work."""
        self.halted = True
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break
        self._reconcile_queued = False
        await self._flatten_now(reason)
        self.sync_state = SyncState.WAIT_FOR_FLAT

        async def recheck() -> None:
            # Runs after any action that was in progress during the flatten.
            if self.adapter.is_connected:
                await self._resync(initial=False)
                if self.book.nonzero_positions() or self.book.active_orders():
                    await self._flatten_now(f"{reason} (re-check)")
            self.sync_state = SyncState.WAIT_FOR_FLAT
            if not keep_halted:
                self.halted = False
            await self._set_status(SyncStatus.WAITING if not keep_halted else SyncStatus.PAUSED)

        self.queue.put_nowait(CallAction(recheck))

    async def _flatten_now(self, reason: str) -> None:
        record = FollowerOrderRecord(
            id=new_id("fo-"), follower_id=self.id, action=FollowerAction.FLATTEN,
            status=RecordStatus.SENT, error=None,
        )
        started = time.perf_counter()
        try:
            await asyncio.wait_for(self.adapter.flatten(), self.engine.settings.order_timeout_s)
            record.status = RecordStatus.OK
            self.flatten_pending = False
            level = LogLevel.WARNING
            message = f"Flattened: {reason}"
        except (BrokerError, asyncio.TimeoutError) as exc:
            record.status = RecordStatus.FAILED
            record.error = str(exc) or type(exc).__name__
            self.flatten_pending = True
            level = LogLevel.ERROR
            message = f"Flatten FAILED ({record.error}): {reason}. Will retry on reconnect"
            if isinstance(exc, NotConnectedError):
                self.supervisor.ensure_connected()
        record.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        record.updated_at = time.time()
        await self._emit(record)
        await self._log(level, LogCategory.KILL_SWITCH, message)

    def release(self) -> None:
        self.halted = False

    # ================================================================== broker calls

    async def _send(
        self,
        request: OrderRequest,
        action: FollowerAction,
        *,
        leader_event_id: str | None = None,
        leader_order_id: str | None = None,
        received: float | None = None,
    ) -> Order | None:
        record = FollowerOrderRecord(
            id=new_id("fo-"), follower_id=self.id, action=action, status=RecordStatus.SENT,
            leader_event_id=leader_event_id, leader_order_id=leader_order_id,
            symbol=request.symbol, side=str(request.side), qty=request.qty,
            order_type=str(request.type), price=request.limit_price or request.stop_price,
        )
        if self.halted:
            return await self._fail(record, RecordStatus.BLOCKED, "Copying halted (kill switch / flatten)")
        if not self.adapter.is_connected:
            self.supervisor.ensure_connected()
            return await self._fail(record, RecordStatus.FAILED, "Follower not connected")
        current = self.book.position(request.symbol) + self._pending(request.symbol)
        reduces = reduces_exposure(current, request.side.sign * request.qty)
        if request.type is OrderType.MARKET and not reduces:
            await self._make_room(request)
        reason = self.engine.risk.check_order(self.id, request, current, reduces)
        if reason:
            await self._log(LogLevel.WARNING, LogCategory.RISK, f"Order blocked: {reason}")
            return await self._fail(record, RecordStatus.BLOCKED, reason)

        started = time.perf_counter()
        try:
            order = await asyncio.wait_for(
                self.adapter.place_order(request), self.engine.settings.order_timeout_s
            )
        except asyncio.TimeoutError:
            self.queue.put_nowait(ResyncAction())
            return await self._fail(record, RecordStatus.FAILED, "Timed out waiting for broker ack")
        except NotConnectedError as exc:
            self.supervisor.ensure_connected()
            return await self._fail(record, RecordStatus.FAILED, str(exc))
        except BrokerError as exc:
            return await self._fail(record, RecordStatus.FAILED, str(exc))

        acked = time.perf_counter()
        record.latency_ms = round((acked - (received if received is not None else started)) * 1000, 2)
        if received is not None:
            self.latencies.append(record.latency_ms)
        record.broker_order_id = order.id
        self.meta[order.id] = OrderMeta(action, record, leader_order_id, started)
        self.book.apply_order(order)
        self._update_record(record, self.book.orders[order.id])
        if order.status is OrderStatus.REJECTED:
            record.error = order.reject_reason
            await self._log(LogLevel.ERROR, LogCategory.ORDER,
                            f"Order rejected: {request.side} {request.qty} {request.symbol}: {order.reject_reason}")
        await self._emit(record)
        await self._emit(StatusChanged(self.id, "order"))
        return order

    async def _fail(self, record: FollowerOrderRecord, status: RecordStatus, error: str) -> None:
        record.status = status
        record.error = error
        record.updated_at = time.time()
        await self._emit(record)
        if status is RecordStatus.FAILED:
            await self._log(LogLevel.ERROR, LogCategory.ORDER,
                            f"Order failed: {record.side} {record.qty} {record.symbol}: {error}")
        return None

    async def _modify(
        self, fo: Order, changes: dict[str, Any], *, leader_event_id: str | None,
        leader_order_id: str | None, received: float | None = None,
    ) -> None:
        record = FollowerOrderRecord(
            id=new_id("fo-"), follower_id=self.id, action=FollowerAction.MODIFY,
            status=RecordStatus.SENT, leader_event_id=leader_event_id,
            leader_order_id=leader_order_id, broker_order_id=fo.id, symbol=fo.symbol,
            side=str(fo.side), qty=changes.get("qty", fo.qty), order_type=str(fo.type),
            price=changes.get("limit_price") or changes.get("stop_price") or fo.price,
        )
        started = time.perf_counter()
        try:
            updated = await asyncio.wait_for(
                self.adapter.modify_order(fo.id, **changes), self.engine.settings.order_timeout_s
            )
            self.book.apply_order(updated)
            record.status = RecordStatus.OK
        except OrderNotWorkingError:
            return  # it filled or was cancelled meanwhile; its own event updates our state
        except (BrokerError, asyncio.TimeoutError) as exc:
            record.status = RecordStatus.FAILED
            record.error = str(exc) or type(exc).__name__
        record.latency_ms = round(
            (time.perf_counter() - (received if received is not None else started)) * 1000, 2
        )
        record.updated_at = time.time()
        await self._emit(record)

    async def _cancel(
        self, fo: Order, reason: str, *, leader_event_id: str | None = None,
        leader_order_id: str | None = None, received: float | None = None,
    ) -> None:
        record = FollowerOrderRecord(
            id=new_id("fo-"), follower_id=self.id, action=FollowerAction.CANCEL,
            status=RecordStatus.SENT, leader_event_id=leader_event_id,
            leader_order_id=leader_order_id, broker_order_id=fo.id, symbol=fo.symbol,
            side=str(fo.side), qty=fo.qty, order_type=str(fo.type), price=fo.price, error=None,
        )
        started = time.perf_counter()
        try:
            updated = await asyncio.wait_for(
                self.adapter.cancel_order(fo.id), self.engine.settings.order_timeout_s
            )
            self.book.apply_order(updated)
            record.status = RecordStatus.OK
            record.error = reason
        except OrderNotWorkingError:
            return  # already filled or cancelled (e.g. by the broker's OCO): nothing to log
        except (BrokerError, asyncio.TimeoutError) as exc:
            record.status = RecordStatus.FAILED
            record.error = str(exc) or type(exc).__name__
        record.latency_ms = round(
            (time.perf_counter() - (received if received is not None else started)) * 1000, 2
        )
        record.updated_at = time.time()
        await self._emit(record)

    # ================================================================== records & logging

    @staticmethod
    def _update_record(record: FollowerOrderRecord, order: Order) -> bool:
        if order.status is OrderStatus.REJECTED:
            status = RecordStatus.REJECTED
        elif order.status is OrderStatus.FILLED:
            status = RecordStatus.FILLED
        elif order.status is OrderStatus.CANCELLED:
            status = RecordStatus.CANCELLED
        else:
            status = RecordStatus.PARTIALLY_FILLED if order.filled_qty else RecordStatus.WORKING
        changed = (
            record.status is not status
            or record.filled_qty != order.filled_qty
            or record.avg_fill_price != order.avg_fill_price
        )
        record.status = status
        record.filled_qty = order.filled_qty
        record.avg_fill_price = order.avg_fill_price
        if order.reject_reason:
            record.error = order.reject_reason
        if changed:
            record.updated_at = time.time()
        return changed

    async def _record_skip(self, event: BrokerEvent, lo: Order, reason: str) -> None:
        symbol = self.cfg.mapping_for(lo.symbol).follower_symbol
        await self._record_skip_raw(event.event_id, lo.id, symbol, reason)

    async def _record_skip_raw(
        self, leader_event_id: str, leader_order_id: str | None, symbol: str, reason: str
    ) -> None:
        await self._emit(FollowerOrderRecord(
            id=new_id("fo-"), follower_id=self.id, action=FollowerAction.SKIPPED,
            status=RecordStatus.SKIPPED, leader_event_id=leader_event_id,
            leader_order_id=leader_order_id, symbol=symbol, error=reason,
        ))

    async def _set_status(self, status: SyncStatus) -> None:
        if status is not self.sync_status:
            self.sync_status = status
            await self._emit(StatusChanged(self.id, f"sync:{status}"))

    async def _emit(self, record: Any) -> None:
        await self.engine.emit(record)

    async def _log(self, level: LogLevel, category: LogCategory, message: str,
                   data: dict | None = None) -> None:
        await self.engine.log(level, category, message, account_id=self.id, data=data)

    # ================================================================== status

    def snapshot(self) -> dict[str, Any]:
        latencies = list(self.latencies)
        targets = self.targets() if self.engine.leader_initialized else {}
        return {
            "account_id": self.id,
            "name": self.cfg.name or self.id,
            "connection": str(self.adapter.status),
            "sync_state": str(self.sync_state),
            "sync_status": str(self.sync_status),
            "symbol_status": dict(self.symbol_status),
            "positions": self.book.nonzero_positions(),
            "targets": {s: q for s, q in targets.items() if q},
            "copy_enabled": self.cfg.copy_enabled,
            "mismatch_mode": str(self.cfg.mismatch_mode),
            "correction_attempts": self.correction_attempts,
            "halted": self.halted,
            "risk_locked": self.engine.risk.is_locked(self.id),
            "reduce_only": self.reduce_only,
            "last_latency_ms": latencies[-1] if latencies else None,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "working_orders": len(self.book.active_orders()),
        }
