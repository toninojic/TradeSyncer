"""Engine sinks: persist records to SQLite and push them to the browser."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import FollowerOrder, LeaderEvent, RiskEvent, SystemLog
from app.engine.events import (
    EngineRecord,
    FollowerOrderRecord,
    LeaderEventRecord,
    LogRecord,
    RiskEventRecord,
    StatusChanged,
)

if TYPE_CHECKING:
    from app.services.runtime import Runtime

log = logging.getLogger(__name__)


def to_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def leader_event_row(r: LeaderEventRecord) -> LeaderEvent:
    return LeaderEvent(
        id=r.id, account_id=to_int(r.account_id) or 0, type=r.type, ts=r.ts,
        received_at=r.received_at, order_id=r.order_id, symbol=r.symbol, side=r.side,
        qty=r.qty, order_type=r.order_type, price=r.price, fill_qty=r.fill_qty,
        fill_price=r.fill_price, leader_position=r.leader_position, copied=r.copied, note=r.note,
    )


def follower_order_row(r: FollowerOrderRecord) -> FollowerOrder:
    return FollowerOrder(
        id=r.id, follower_id=to_int(r.follower_id) or 0, leader_event_id=r.leader_event_id,
        leader_order_id=r.leader_order_id, action=str(r.action), status=str(r.status),
        broker_order_id=r.broker_order_id, symbol=r.symbol, side=r.side, qty=r.qty,
        order_type=r.order_type, price=r.price, filled_qty=r.filled_qty,
        avg_fill_price=r.avg_fill_price, latency_ms=r.latency_ms, error=r.error,
        created_at=r.created_at, updated_at=r.updated_at,
    )


def log_row(r: LogRecord) -> SystemLog:
    return SystemLog(ts=r.ts, level=str(r.level), category=str(r.category),
                     account_id=to_int(r.account_id), message=r.message, data=r.data)


def risk_event_row(r: RiskEventRecord) -> RiskEvent:
    return RiskEvent(account_id=to_int(r.follower_id) or 0, rule=r.rule, message=r.message,
                     trading_day=r.trading_day, ts=r.ts)


def record_payload(record: EngineRecord) -> dict[str, Any] | None:
    """JSON message for the browser (ids normalised to the DB's integer ids)."""
    if isinstance(record, LeaderEventRecord):
        data = record.to_dict()
        data["account_id"] = to_int(record.account_id)
        return {"type": "leader_event", "data": data}
    if isinstance(record, FollowerOrderRecord):
        data = record.to_dict()
        data["follower_id"] = to_int(record.follower_id)
        data["action"] = str(record.action)
        data["status"] = str(record.status)
        return {"type": "follower_order", "data": data}
    if isinstance(record, LogRecord):
        data = record.to_dict()
        data["account_id"] = to_int(record.account_id)
        data["level"] = str(record.level)
        data["category"] = str(record.category)
        return {"type": "log", "data": data}
    if isinstance(record, RiskEventRecord):
        data = record.to_dict()
        data["account_id"] = to_int(record.follower_id)
        del data["follower_id"]
        return {"type": "risk_event", "data": data}
    return None


class DbSink:
    """Writes engine records in small batches from a background task, so the
    engine never waits for the disk."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker
        self._queue: asyncio.Queue[EngineRecord] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._idle = asyncio.Event()
        self._idle.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._writer(), name="db-writer")

    async def emit(self, record: EngineRecord) -> None:
        if isinstance(record, StatusChanged):
            return
        self._idle.clear()
        self._queue.put_nowait(record)

    async def flush(self, timeout: float = 5.0) -> None:
        if self._queue.empty() and self._idle.is_set():
            return
        await asyncio.wait_for(self._idle.wait(), timeout)

    async def stop(self) -> None:
        try:
            await self.flush()
        except asyncio.TimeoutError:
            log.warning("DB writer did not flush in time")
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _writer(self) -> None:
        while True:
            record = await self._queue.get()
            batch = [record]
            while not self._queue.empty() and len(batch) < 500:
                batch.append(self._queue.get_nowait())
            try:
                async with self._sessionmaker() as session:
                    for item in batch:
                        if isinstance(item, LeaderEventRecord):
                            await session.merge(leader_event_row(item))
                        elif isinstance(item, FollowerOrderRecord):
                            await session.merge(follower_order_row(item))
                        elif isinstance(item, LogRecord):
                            session.add(log_row(item))
                        elif isinstance(item, RiskEventRecord):
                            session.add(risk_event_row(item))
                    await session.commit()
            except Exception:
                log.exception("Failed to write %d engine records", len(batch))
            if self._queue.empty():
                self._idle.set()


class HubSink:
    """Pushes records to the WebSocket hub and marks the dashboard snapshot dirty."""

    def __init__(self, runtime: "Runtime") -> None:
        self._runtime = runtime

    async def emit(self, record: EngineRecord) -> None:
        if isinstance(record, StatusChanged):
            self._runtime.mark_dirty()
            return
        payload = record_payload(record)
        if payload is not None:
            self._runtime.hub.publish(payload)
        if isinstance(record, (FollowerOrderRecord, RiskEventRecord)):
            self._runtime.mark_dirty()
