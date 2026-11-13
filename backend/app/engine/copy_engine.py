"""The copy engine: subscribes to the leader and fans events out to follower workers."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import Any

from app.brokers.base import (
    BrokerAdapter,
    BrokerError,
    BrokerEvent,
    BrokerEventType,
    ConnectionStatus,
)
from app.engine.config import EngineSettings, FollowerConfig
from app.engine.connection import ConnectionSupervisor
from app.engine.events import (
    EngineRecord,
    EngineSink,
    LeaderEventRecord,
    LogCategory,
    LogLevel,
    LogRecord,
    MemorySink,
    StatusChanged,
)
from app.engine.follower_worker import (
    FollowerWorker,
    JoinAction,
    LeaderEventAction,
    ReconcileTick,
)
from app.engine.risk import AllowAll, RiskGuard
from app.engine.state import Deduper, LeaderBook

log = logging.getLogger(__name__)

_LOG_LEVELS = {LogLevel.INFO: logging.INFO, LogLevel.WARNING: logging.WARNING, LogLevel.ERROR: logging.ERROR}


class CopyEngine:
    def __init__(
        self,
        leader: BrokerAdapter,
        followers: Iterable[tuple[BrokerAdapter, FollowerConfig]] = (),
        *,
        sink: EngineSink | None = None,
        settings: EngineSettings | None = None,
        risk: RiskGuard | None = None,
    ) -> None:
        self.leader_adapter = leader
        self.leader = LeaderBook()
        self.leader_initialized = False
        self.settings = settings or EngineSettings()
        self.sink: EngineSink = sink or MemorySink()
        self.risk: RiskGuard = risk or AllowAll()
        self.workers: dict[str, FollowerWorker] = {}
        self.running = False
        self.killed = False
        self.leader_fill_seq = 0  # increments on every leader fill event
        self._dedup = Deduper(self.settings.dedup_window)
        self._leader_stream = None
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self.leader_supervisor = ConnectionSupervisor(
            leader, on_connected=self._on_leader_connected,
            on_attempt_failed=self._on_leader_connect_failed,
        )
        for adapter, cfg in followers:
            cfg.validate()
            self.workers[adapter.account_id] = FollowerWorker(self, adapter, cfg)

    # ================================================================== lifecycle

    async def start(self, *, running: bool = True, reconcile_loop: bool = True) -> None:
        if self._started:
            return
        self._started = True
        self._leader_stream = self.leader_adapter.subscribe()
        self._tasks.append(asyncio.create_task(self._leader_pump(), name="leader-pump"))
        for worker in self.workers.values():
            await worker.start()
        self.running = running
        self.leader_supervisor.ensure_connected()
        if reconcile_loop:
            self._tasks.append(asyncio.create_task(self._reconcile_loop(), name="reconcile-loop"))
        await self.log(LogLevel.INFO, LogCategory.ENGINE,
                       f"Copy engine started ({'running' if running else 'paused'})")

    async def stop(self, *, disconnect: bool = True) -> None:
        await self.leader_supervisor.stop()
        for worker in self.workers.values():
            await worker.stop(disconnect=disconnect)
        if self._leader_stream:
            self._leader_stream.close()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        if disconnect and self.leader_adapter.is_connected:
            try:
                await self.leader_adapter.disconnect()
            except BrokerError:
                pass
        self._started = False
        self.running = False

    # ================================================================== controls

    async def pause(self) -> None:
        self.running = False
        await self.log(LogLevel.WARNING, LogCategory.ENGINE, "Copying paused")
        await self.emit(StatusChanged(None, "paused"))

    async def resume(self) -> None:
        self.killed = False
        self.running = True
        for worker in self.workers.values():
            worker.release()
            worker.submit(JoinAction())
        await self.log(LogLevel.INFO, LogCategory.ENGINE, "Copying resumed")
        await self.emit(StatusChanged(None, "resumed"))

    async def kill_switch(self, reason: str = "Kill switch activated") -> None:
        """Stop copying and flatten every follower."""
        self.running = False
        self.killed = True
        await self.log(LogLevel.ERROR, LogCategory.KILL_SWITCH, reason)
        await asyncio.gather(
            *(w.emergency_flatten(reason, keep_halted=True) for w in self.workers.values())
        )
        await self.emit(StatusChanged(None, "kill_switch"))

    async def flatten_follower(self, account_id: str, reason: str = "Manual flatten") -> None:
        """Flatten one follower. It re-joins copying once the leader is flat."""
        worker = self.workers[account_id]
        await worker.emergency_flatten(reason, keep_halted=False)

    async def retry_sync(self, account_id: str) -> None:
        """Reset a follower that gave up auto-correcting and reconcile it now."""
        await self.log(LogLevel.INFO, LogCategory.RECONCILE, "Manual retry sync", account_id=account_id)
        self.workers[account_id].retry_sync()

    async def reconcile_now(self) -> None:
        if self.leader_adapter.is_connected and self.leader_initialized:
            await self._resync_leader(initial=False)
        elif not self.leader_adapter.is_connected:
            self.leader_supervisor.ensure_connected()
        for worker in self.workers.values():
            worker.submit(ReconcileTick())

    # ================================================================== follower management

    async def add_follower(self, adapter: BrokerAdapter, cfg: FollowerConfig) -> FollowerWorker:
        cfg.validate()
        if adapter.account_id in self.workers:
            raise ValueError(f"Follower {adapter.account_id} already exists")
        worker = FollowerWorker(self, adapter, cfg)
        self.workers[adapter.account_id] = worker
        if self._started:
            await worker.start()
        return worker

    async def remove_follower(self, account_id: str) -> None:
        worker = self.workers.pop(account_id, None)
        if worker is not None:
            await worker.stop()

    def update_follower(self, cfg: FollowerConfig) -> None:
        cfg.validate()
        self.workers[cfg.account_id].update_config(cfg)

    # ================================================================== leader

    async def _leader_pump(self) -> None:
        assert self._leader_stream is not None
        async for event in self._leader_stream:
            try:
                await self._handle_leader_event(event, time.perf_counter())
            except Exception as exc:
                log.exception("Leader event handling failed")
                await self.log(LogLevel.ERROR, LogCategory.ENGINE, f"Leader event error: {exc!r}")

    async def _handle_leader_event(self, event: BrokerEvent, received: float) -> None:
        if event.type is BrokerEventType.CONNECTION_STATUS:
            await self._on_leader_status(event)
            return
        if not self._dedup.is_new(event.event_id):
            await self.log(LogLevel.WARNING, LogCategory.ENGINE, "Duplicate leader event ignored",
                           account_id=self.leader_adapter.account_id,
                           data={"event_id": event.event_id, "type": str(event.type)})
            return
        if event.type is BrokerEventType.ORDER_FILLED:
            self.leader_fill_seq += 1
        if event.order is not None:
            if event.type is BrokerEventType.ORDER_PLACED:
                self.leader.classify(event.order)  # uses the position *before* this order
            self.leader.apply_order(event.order)
        if event.type is BrokerEventType.POSITION_CHANGED:
            await self.emit(StatusChanged(self.leader_adapter.account_id, "leader_position"))
            return

        copied = self.running and self.leader_initialized and not self.killed
        note = None
        if not self.leader_initialized:
            note = "Leader state not loaded yet"
        elif not self.running:
            note = "Copier paused"
        await self.emit(self._leader_record(event, copied, note))
        if not copied:
            return
        positions = dict(self.leader.positions)
        flat = self.leader.is_flat()
        for worker in self.workers.values():
            worker.submit(LeaderEventAction(event, received, positions, flat, self.leader_fill_seq))

    def _leader_record(self, event: BrokerEvent, copied: bool, note: str | None) -> LeaderEventRecord:
        order = event.order
        fill = event.fill
        symbol = order.symbol if order else (fill.symbol if fill else None)
        return LeaderEventRecord(
            id=event.event_id,
            account_id=event.account_id,
            type=str(event.type),
            ts=event.ts,
            received_at=time.time(),
            order_id=order.id if order else None,
            symbol=symbol,
            side=str(order.side) if order else None,
            qty=order.qty if order else None,
            order_type=str(order.type) if order else None,
            price=order.price if order else None,
            fill_qty=fill.qty if fill else None,
            fill_price=fill.price if fill else None,
            leader_position=self.leader.position(symbol) if symbol else None,
            copied=copied,
            note=note or event.message,
        )

    async def _on_leader_status(self, event: BrokerEvent) -> None:
        status = event.connection
        if status in (ConnectionStatus.DISCONNECTED, ConnectionStatus.ERROR):
            await self.log(LogLevel.WARNING, LogCategory.CONNECTION,
                           f"Leader disconnected: {event.message or status}",
                           account_id=self.leader_adapter.account_id)
            self.leader_supervisor.ensure_connected()
        await self.emit(StatusChanged(self.leader_adapter.account_id, f"connection:{status}"))

    async def _on_leader_connected(self) -> None:
        await self.log(LogLevel.INFO, LogCategory.CONNECTION, "Leader connected",
                       account_id=self.leader_adapter.account_id)
        await self._resync_leader(initial=not self.leader_initialized)

    async def _on_leader_connect_failed(self, exc: Exception, delay: float, attempt: int) -> None:
        await self.log(LogLevel.WARNING, LogCategory.CONNECTION,
                       f"Leader reconnect attempt {attempt} failed ({exc}); retrying in {delay:.1f}s",
                       account_id=self.leader_adapter.account_id)

    async def _resync_leader(self, *, initial: bool) -> None:
        try:
            orders = await self.leader_adapter.get_orders(active_only=False)
            positions = await self.leader_adapter.get_positions()
        except BrokerError as exc:
            await self.log(LogLevel.WARNING, LogCategory.RECONCILE, f"Leader snapshot failed: {exc}",
                           account_id=self.leader_adapter.account_id)
            return
        if initial:
            self.leader.load_initial(orders, positions)
            for order in orders:
                if order.is_active:
                    self.leader.classify(order)
            self.leader_initialized = True
            for worker in self.workers.values():
                worker.submit(JoinAction())
            return
        self.leader.apply_snapshot_orders(orders)
        for symbol, local, broker in self.leader.verify_positions(positions):
            await self.log(LogLevel.WARNING, LogCategory.RECONCILE,
                           f"Leader {symbol}: local position {local} differed from broker {broker}; "
                           "using broker value", account_id=self.leader_adapter.account_id)
        for worker in self.workers.values():
            worker.submit(ReconcileTick())

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.reconcile_interval_s)
            try:
                await self.reconcile_now()
            except Exception:
                log.exception("Reconcile tick failed")

    # ================================================================== sink & status

    async def emit(self, record: EngineRecord) -> None:
        try:
            await self.sink.emit(record)
        except Exception:  # the sink must never break trading
            log.exception("Engine sink failed")

    async def log(
        self,
        level: LogLevel,
        category: LogCategory,
        message: str,
        *,
        account_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        log.log(_LOG_LEVELS[level], "[%s] %s %s", category, account_id or "-", message)
        await self.emit(LogRecord(level, category, message, account_id, data))

    def is_idle(self) -> bool:
        if self._leader_stream is not None and self._leader_stream.pending:
            return False
        return not any(w.is_busy() for w in self.workers.values())

    async def wait_idle(self, timeout: float = 5.0) -> None:
        """Wait until every queued event and action has been processed (tests, UI)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        stable = 0
        while loop.time() < deadline:
            if self.is_idle():
                stable += 1
                if stable >= 3:
                    return
            else:
                stable = 0
            await asyncio.sleep(0.005)
        raise TimeoutError("Copy engine did not become idle")

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "killed": self.killed,
            "leader": {
                "account_id": self.leader_adapter.account_id,
                "connection": str(self.leader_adapter.status),
                "initialized": self.leader_initialized,
                "positions": self.leader.nonzero_positions(),
                "working_orders": len(self.leader.active_orders()),
            },
            "followers": [w.snapshot() for w in self.workers.values()],
        }
