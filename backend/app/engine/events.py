"""Records the engine emits for the trade log, system log and live UI.

The engine never writes to the database directly; it hands records to an
:class:`EngineSink`. Tests use :class:`MemorySink`; the API layer (M2) plugs
in a sink that persists to SQLite and broadcasts over WebSocket.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class LogLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogCategory(StrEnum):
    CONNECTION = "connection"
    RECONCILE = "reconcile"
    RISK = "risk"
    ENGINE = "engine"
    ORDER = "order"
    KILL_SWITCH = "kill_switch"


class FollowerAction(StrEnum):
    COPY_FILL = "copy_fill"  # market order mirroring a leader fill
    MIRROR_ENTRY = "mirror_entry"  # resting limit/stop entry mirrored from the leader
    MIRROR_EXIT = "mirror_exit"  # SL/TP mirrored from the leader
    MODIFY = "modify"
    CANCEL = "cancel"
    RECONCILE = "reconcile"  # correction sent by the reconciliation loop
    FLATTEN = "flatten"
    SKIPPED = "skipped"  # nothing sent (size 0, disabled, ...)


class RecordStatus(StrEnum):
    SENT = "sent"
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"  # request failed (disconnected, timeout, broker error)
    BLOCKED = "blocked"  # blocked by a risk rule
    SKIPPED = "skipped"
    OK = "ok"  # modify/cancel/flatten succeeded


@dataclass(slots=True)
class LeaderEventRecord:
    id: str  # the broker event id (unique)
    account_id: str
    type: str
    ts: float  # broker timestamp
    received_at: float  # local wall clock
    order_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    qty: int | None = None
    order_type: str | None = None
    price: float | None = None
    fill_qty: int | None = None
    fill_price: float | None = None
    leader_position: int | None = None
    copied: bool = True
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FollowerOrderRecord:
    id: str
    follower_id: str
    action: FollowerAction
    status: RecordStatus
    leader_event_id: str | None = None
    leader_order_id: str | None = None
    broker_order_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    qty: int | None = None
    order_type: str | None = None
    price: float | None = None
    filled_qty: int = 0
    avg_fill_price: float | None = None
    latency_ms: float | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LogRecord:
    level: LogLevel
    category: LogCategory
    message: str
    account_id: str | None = None
    data: dict[str, Any] | None = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RiskEventRecord:
    """A risk rule was triggered (shown on the Risk page)."""

    follower_id: str
    rule: str  # daily_loss_limit | max_position | trading_hours
    message: str
    trading_day: str  # YYYY-MM-DD (CME trade date)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StatusChanged:
    """Something visible on the dashboard changed (positions, sync status, ...)."""

    account_id: str | None = None
    reason: str = ""


EngineRecord = LeaderEventRecord | FollowerOrderRecord | LogRecord | RiskEventRecord | StatusChanged


class EngineSink(Protocol):
    async def emit(self, record: EngineRecord) -> None: ...


class MemorySink:
    """Keeps every record in memory. Used by tests and as a default."""

    def __init__(self) -> None:
        self.leader_events: list[LeaderEventRecord] = []
        self.follower_orders: dict[str, FollowerOrderRecord] = {}
        self.logs: list[LogRecord] = []
        self.risk_events: list[RiskEventRecord] = []
        self.status_changes = 0

    async def emit(self, record: EngineRecord) -> None:
        if isinstance(record, LeaderEventRecord):
            self.leader_events.append(record)
        elif isinstance(record, FollowerOrderRecord):
            self.follower_orders[record.id] = record
        elif isinstance(record, LogRecord):
            self.logs.append(record)
        elif isinstance(record, RiskEventRecord):
            self.risk_events.append(record)
        else:
            self.status_changes += 1

    # convenience helpers for tests -------------------------------------------------

    def orders_for(self, follower_id: str) -> list[FollowerOrderRecord]:
        return [r for r in self.follower_orders.values() if r.follower_id == follower_id]

    def logs_in(self, category: LogCategory) -> list[LogRecord]:
        return [r for r in self.logs if r.category is category]


class FanoutSink:
    """Send every record to several sinks."""

    def __init__(self, *sinks: EngineSink) -> None:
        self.sinks = list(sinks)

    async def emit(self, record: EngineRecord) -> None:
        for sink in self.sinks:
            await sink.emit(record)
