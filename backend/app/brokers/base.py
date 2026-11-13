"""Broker-agnostic interface and data models.

Every broker integration (simulated, Tradovate, Rithmic, MT5 ...) implements
:class:`BrokerAdapter`. The copy engine only talks to this interface, so a new
broker can be added without touching the engine.
"""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Callable


# --------------------------------------------------------------------------- enums


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @staticmethod
    def from_sign(value: int) -> "Side":
        if value == 0:
            raise ValueError("Cannot derive a side from zero")
        return Side.BUY if value > 0 else Side.SELL


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderStatus(StrEnum):
    WORKING = "working"  # accepted by the broker, not (fully) filled yet
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})


class ConnectionStatus(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class BrokerEventType(StrEnum):
    ORDER_PLACED = "order_placed"
    ORDER_MODIFIED = "order_modified"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REJECTED = "order_rejected"
    ORDER_FILLED = "order_filled"  # one event per fill (partial fills produce several)
    POSITION_CHANGED = "position_changed"
    CONNECTION_STATUS = "connection_status"


# --------------------------------------------------------------------------- models


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    side: Side
    qty: int
    type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    # Bracket: protective orders the broker creates once this order fills.
    stop_loss: float | None = None
    take_profit: float | None = None
    # Orders that share an OCO group cancel each other when one fills.
    oco_group: str | None = None
    # Idempotency key. Placing a second order with the same key returns the first one.
    client_order_id: str | None = None


@dataclass(slots=True)
class Order:
    id: str
    account_id: str
    symbol: str
    side: Side
    qty: int
    type: OrderType
    status: OrderStatus
    limit_price: float | None = None
    stop_price: float | None = None
    filled_qty: int = 0
    avg_fill_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    parent_id: str | None = None
    oco_group: str | None = None
    client_order_id: str | None = None
    reject_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def remaining(self) -> int:
        return max(0, self.qty - self.filled_qty)

    @property
    def is_active(self) -> bool:
        return self.status is OrderStatus.WORKING

    @property
    def price(self) -> float | None:
        """The order's trigger/limit price, whichever applies."""
        if self.type is OrderType.LIMIT:
            return self.limit_price
        if self.type is OrderType.STOP:
            return self.stop_price
        return None

    def snapshot(self) -> "Order":
        return copy.copy(self)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Position:
    account_id: str
    symbol: str
    qty: int  # signed: positive = long, negative = short
    avg_price: float = 0.0
    market_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0  # realized today for this symbol

    def snapshot(self) -> "Position":
        return copy.copy(self)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Fill:
    id: str
    order_id: str
    account_id: str
    symbol: str
    side: Side
    qty: int
    price: float
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AccountSnapshot:
    account_id: str
    balance: float
    realized_pnl_today: float
    unrealized_pnl: float  # open P&L measured from the entry price
    status: ConnectionStatus
    # Open P&L measured from the start of the trading day (the price at the daily
    # reset for positions carried over). None = same as unrealized_pnl.
    day_unrealized_pnl: float | None = None

    @property
    def day_pnl(self) -> float:
        """Realized + unrealized P&L of the current trading day."""
        open_pnl = self.unrealized_pnl if self.day_unrealized_pnl is None else self.day_unrealized_pnl
        return round(self.realized_pnl_today + open_pnl, 2)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["day_pnl"] = self.day_pnl
        return d


@dataclass(slots=True)
class BrokerEvent:
    event_id: str  # unique per event; used for idempotency
    type: BrokerEventType
    account_id: str
    ts: float = field(default_factory=time.time)
    order: Order | None = None
    fill: Fill | None = None
    position: Position | None = None
    connection: ConnectionStatus | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- errors


class BrokerError(Exception):
    """Base class for broker errors."""


class NotConnectedError(BrokerError):
    """The account is not connected; the request was not sent."""


class BrokerConnectionError(BrokerError):
    """Connecting to the broker failed."""


class OrderNotFoundError(BrokerError):
    pass


class OrderNotWorkingError(BrokerError):
    """Modify/cancel on an order that is already filled, cancelled or rejected."""


class InvalidOrderError(BrokerError):
    pass


# --------------------------------------------------------------------------- event stream


class EventStream:
    """An async iterator of broker events for one subscriber."""

    def __init__(self, on_close: Callable[["EventStream"], None] | None = None) -> None:
        self._queue: asyncio.Queue[BrokerEvent | None] = asyncio.Queue()
        self._closed = False
        self._on_close = on_close

    def push(self, event: BrokerEvent) -> None:
        if not self._closed:
            self._queue.put_nowait(event)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)
        if self._on_close:
            self._on_close(self)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def __aiter__(self) -> "EventStream":
        return self

    async def __anext__(self) -> BrokerEvent:
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event


class EventPublisher:
    """Fan-out helper adapters can use to implement :meth:`BrokerAdapter.subscribe`."""

    def __init__(self) -> None:
        self._streams: list[EventStream] = []

    def subscribe(self) -> EventStream:
        stream = EventStream(on_close=self._remove)
        self._streams.append(stream)
        return stream

    def publish(self, event: BrokerEvent) -> None:
        for stream in list(self._streams):
            stream.push(event)

    def close_all(self) -> None:
        for stream in list(self._streams):
            stream.close()

    def _remove(self, stream: EventStream) -> None:
        if stream in self._streams:
            self._streams.remove(stream)


# --------------------------------------------------------------------------- adapter


class BrokerAdapter(ABC):
    """Interface every broker integration implements.

    Contract:
    * ``place_order`` returns the order as acknowledged by the broker. A rejected
      order is returned with ``status == REJECTED`` (it does not raise).
    * Calls made while disconnected raise :class:`NotConnectedError`.
    * ``subscribe`` returns a stream of :class:`BrokerEvent`. Every event has a
      unique ``event_id``. Events that happen while the connection is down may be
      lost; consumers must resync with ``get_positions``/``get_orders`` after a
      reconnect.
    """

    broker_type: str = "abstract"

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id

    @property
    @abstractmethod
    def status(self) -> ConnectionStatus: ...

    @property
    def is_connected(self) -> bool:
        return self.status is ConnectionStatus.CONNECTED

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def get_account(self) -> AccountSnapshot: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_orders(self, active_only: bool = True) -> list[Order]: ...

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> Order: ...

    @abstractmethod
    async def modify_order(
        self,
        order_id: str,
        *,
        qty: int | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> Order: ...

    @abstractmethod
    async def flatten(self, symbol: str | None = None) -> None:
        """Cancel all working orders and close all positions (optionally for one symbol)."""

    @abstractmethod
    def subscribe(self) -> EventStream: ...
