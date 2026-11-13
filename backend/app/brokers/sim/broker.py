"""A fully working in-memory broker used for development and testing.

Orders are executed against :class:`SimMarket` prices (paper-trading style):
* market orders fill immediately at the current price,
* resting limit orders fill at their limit price when the market reaches it,
* stop orders trigger when the market reaches the stop and fill at market,
* bracket orders (``stop_loss``/``take_profit``) create an OCO pair of exit
  orders as soon as the entry fills.

Failure injection (see :class:`FailureInjection`) lets tests and the simulator
panel reproduce disconnects, rejections, partial fills, latency and duplicate
events.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass

from app.brokers.base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerConnectionError,
    BrokerEvent,
    BrokerEventType,
    ConnectionStatus,
    EventPublisher,
    EventStream,
    Fill,
    InvalidOrderError,
    NotConnectedError,
    Order,
    OrderNotFoundError,
    OrderNotWorkingError,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    new_id,
)
from app.brokers.sim.market import SimMarket
from app.core.contracts import UnknownSymbolError, get_spec, pnl, round_to_tick

log = logging.getLogger(__name__)


@dataclass
class FailureInjection:
    reject_next: int = 0
    reject_reason: str = "Simulated rejection"
    partial_next: int = 0
    partial_ratio: float = 0.5
    # Seconds until the rest of a partially filled order fills.
    # None keeps the remainder working until release_partial_fills() is called.
    partial_remainder_delay: float | None = 1.0
    latency_s: float = 0.0
    duplicate_next_events: int = 0
    forced_down: bool = False
    forced_down_until: float | None = None  # time.monotonic() deadline

    def is_down(self) -> bool:
        if not self.forced_down:
            return False
        if self.forced_down_until is not None and time.monotonic() >= self.forced_down_until:
            self.forced_down = False
            self.forced_down_until = None
            return False
        return True

    def to_dict(self) -> dict:
        remaining = None
        if self.forced_down and self.forced_down_until is not None:
            remaining = max(0.0, round(self.forced_down_until - time.monotonic(), 1))
        return {
            "reject_next": self.reject_next,
            "reject_reason": self.reject_reason,
            "partial_next": self.partial_next,
            "partial_ratio": self.partial_ratio,
            "partial_remainder_delay": self.partial_remainder_delay,
            "latency_ms": round(self.latency_s * 1000),
            "duplicate_next_events": self.duplicate_next_events,
            "forced_down": self.is_down(),
            "forced_down_remaining_s": remaining,
        }


@dataclass
class _PositionState:
    qty: int = 0
    avg_price: float = 0.0
    realized_today: float = 0.0
    # Cost basis for the *day* P&L: the entry price, or the price at the daily
    # reset for a position that was carried into the new trading day.
    day_price: float = 0.0


@dataclass
class _PartialPlan:
    ratio: float
    remainder_delay: float | None
    done: bool = False


class SimulatedBroker(BrokerAdapter):
    broker_type = "sim"

    def __init__(
        self,
        account_id: str,
        market: SimMarket,
        *,
        starting_balance: float = 50_000.0,
    ) -> None:
        super().__init__(account_id)
        self.market = market
        self.failures = FailureInjection()
        self._status = ConnectionStatus.DISCONNECTED
        self._publisher = EventPublisher()
        self._orders: dict[str, Order] = {}
        self._client_ids: dict[str, str] = {}
        self._positions: dict[str, _PositionState] = {}
        self._fills: list[Fill] = []
        self._partials: dict[str, _PartialPlan] = {}
        self._held: set[str] = set()  # orders whose remainder waits for release
        self._starting_balance = starting_balance
        self._realized_total = 0.0
        self._realized_today = 0.0
        market.add_listener(self._on_price)

    # ================================================================== connection

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    async def connect(self) -> None:
        self._set_status(ConnectionStatus.CONNECTING)
        await self._sleep_latency()
        if self.failures.is_down():
            self._set_status(ConnectionStatus.ERROR, "Simulated connection failure")
            raise BrokerConnectionError(f"{self.account_id}: simulated connection failure")
        self._set_status(ConnectionStatus.CONNECTED)

    async def disconnect(self) -> None:
        self._set_status(ConnectionStatus.DISCONNECTED, "Disconnected by client")

    def subscribe(self) -> EventStream:
        return self._publisher.subscribe()

    def close(self) -> None:
        """Detach from the market and end all event streams (account removed)."""
        self.market.remove_listener(self._on_price)
        self._status = ConnectionStatus.DISCONNECTED
        self._publisher.close_all()

    # ================================================================== failure injection

    def inject_disconnect(self, duration_s: float | None = None) -> None:
        """Drop the connection. Reconnects fail until ``duration_s`` passes or
        :meth:`restore_connection` is called (``None`` = until restored)."""
        self.failures.forced_down = True
        self.failures.forced_down_until = (
            time.monotonic() + duration_s if duration_s is not None else None
        )
        if self._status is not ConnectionStatus.DISCONNECTED:
            self._set_status(ConnectionStatus.DISCONNECTED, "Simulated connection drop")

    def restore_connection(self) -> None:
        """Allow reconnecting again (the client still has to call ``connect``)."""
        self.failures.forced_down = False
        self.failures.forced_down_until = None

    def reject_next_orders(self, count: int = 1, reason: str = "Simulated rejection") -> None:
        self.failures.reject_next = count
        self.failures.reject_reason = reason

    def partial_fill_next_orders(
        self, count: int = 1, ratio: float = 0.5, remainder_delay: float | None = 1.0
    ) -> None:
        self.failures.partial_next = count
        self.failures.partial_ratio = ratio
        self.failures.partial_remainder_delay = remainder_delay

    def partial_fill_order(
        self, order_id: str, ratio: float = 0.5, remainder_delay: float | None = None
    ) -> None:
        """Make an already working (resting) order fill only partly when it triggers."""
        self._get_active(order_id)
        self._partials[order_id] = _PartialPlan(ratio, remainder_delay)

    def set_latency(self, ms: float) -> None:
        self.failures.latency_s = max(0.0, ms) / 1000.0

    def duplicate_next_events(self, count: int = 1) -> None:
        self.failures.duplicate_next_events = count

    def release_partial_fills(self) -> None:
        for order_id in list(self._held):
            self._release(order_id)

    def clear_failures(self) -> None:
        self.failures = FailureInjection()

    def reset_day(self) -> None:
        """Start a new trading day: the day P&L restarts at zero. Open positions
        are carried at the current price for day-P&L purposes (like a daily
        settlement); their entry price and the account balance are unchanged."""
        self._realized_today = 0.0
        for symbol, pos in self._positions.items():
            pos.realized_today = 0.0
            if pos.qty:
                pos.day_price = self.market.price(symbol)

    # ================================================================== queries

    async def get_account(self) -> AccountSnapshot:
        await self._request_preamble()
        return self.account_snapshot()

    def account_snapshot(self) -> AccountSnapshot:
        """Synchronous snapshot for the simulator UI; works even when disconnected."""
        return AccountSnapshot(
            account_id=self.account_id,
            balance=round(self._starting_balance + self._realized_total, 2),
            realized_pnl_today=round(self._realized_today, 2),
            unrealized_pnl=round(sum(p.unrealized_pnl for p in self.positions_snapshot()), 2),
            status=self._status,
            day_unrealized_pnl=round(sum(
                pnl(symbol, st.day_price, self.market.price(symbol), st.qty)
                for symbol, st in self._positions.items() if st.qty
            ), 2),
        )

    async def get_positions(self) -> list[Position]:
        await self._request_preamble()
        return self.positions_snapshot()

    def positions_snapshot(self) -> list[Position]:
        return [self._position(s, st) for s, st in self._positions.items() if st.qty != 0]

    async def get_orders(self, active_only: bool = True) -> list[Order]:
        await self._request_preamble()
        return self.orders_snapshot(active_only)

    def orders_snapshot(self, active_only: bool = True) -> list[Order]:
        orders = self._orders.values()
        return [o.snapshot() for o in orders if o.is_active or not active_only]

    def fills_snapshot(self) -> list[Fill]:
        return list(self._fills)

    # ================================================================== trading

    async def place_order(self, request: OrderRequest) -> Order:
        await self._request_preamble()
        if request.client_order_id and request.client_order_id in self._client_ids:
            return self._orders[self._client_ids[request.client_order_id]].snapshot()
        return self._submit(request).snapshot()

    async def modify_order(
        self,
        order_id: str,
        *,
        qty: int | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        await self._request_preamble()
        order = self._get_active(order_id)
        tick = get_spec(order.symbol).tick_size
        if qty is not None:
            if qty <= order.filled_qty:
                raise InvalidOrderError(
                    f"New qty {qty} must be greater than filled qty {order.filled_qty}"
                )
            order.qty = int(qty)
        if limit_price is not None:
            if order.type is not OrderType.LIMIT:
                raise InvalidOrderError("limit_price can only be changed on a limit order")
            order.limit_price = round_to_tick(limit_price, tick)
        if stop_price is not None:
            if order.type is not OrderType.STOP:
                raise InvalidOrderError("stop_price can only be changed on a stop order")
            order.stop_price = round_to_tick(stop_price, tick)
        order.updated_at = time.time()
        self._emit(BrokerEventType.ORDER_MODIFIED, order=order)
        self._try_execute(order, on_arrival=True)
        return order.snapshot()

    async def cancel_order(self, order_id: str) -> Order:
        await self._request_preamble()
        order = self._get_active(order_id)
        self._cancel(order, "Cancelled by client")
        return order.snapshot()

    async def flatten(self, symbol: str | None = None) -> None:
        await self._request_preamble()
        self.flatten_now(symbol)

    def flatten_now(self, symbol: str | None = None) -> None:
        """Synchronous flatten (exchange side); ignores injected failures."""
        for order in list(self._orders.values()):
            if order.is_active and (symbol is None or order.symbol == symbol):
                self._cancel(order, "Cancelled by flatten")
        for sym, state in list(self._positions.items()):
            if state.qty != 0 and (symbol is None or sym == symbol):
                self._submit(
                    OrderRequest(sym, Side.from_sign(-state.qty), abs(state.qty)),
                    ignore_failures=True,
                )

    # ================================================================== internals

    async def _request_preamble(self) -> None:
        self._require_connected()
        await self._sleep_latency()
        self._require_connected()

    async def _sleep_latency(self) -> None:
        if self.failures.latency_s > 0:
            await asyncio.sleep(self.failures.latency_s)

    def _require_connected(self) -> None:
        if self._status is not ConnectionStatus.CONNECTED:
            raise NotConnectedError(f"{self.account_id} is not connected")

    def _set_status(self, status: ConnectionStatus, message: str | None = None) -> None:
        if status is self._status and message is None:
            return
        self._status = status
        self._emit(BrokerEventType.CONNECTION_STATUS, connection=status, message=message, force=True)

    def _emit(self, event_type: BrokerEventType, *, force: bool = False, **payload) -> None:
        # Events that happen while the connection is down never reach the client.
        if not force and self._status is not ConnectionStatus.CONNECTED:
            return
        if "order" in payload and payload["order"] is not None:
            payload["order"] = payload["order"].snapshot()
        event = BrokerEvent(
            event_id=new_id("evt-"), type=event_type, account_id=self.account_id, **payload
        )
        self._publisher.publish(event)
        if self.failures.duplicate_next_events > 0 and event_type is not BrokerEventType.CONNECTION_STATUS:
            self.failures.duplicate_next_events -= 1
            self._publisher.publish(event)

    def _get_active(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        if not order.is_active:
            raise OrderNotWorkingError(f"Order {order_id} is {order.status}")
        return order

    def _submit(self, request: OrderRequest, *, ignore_failures: bool = False,
                parent_id: str | None = None) -> Order:
        order = Order(
            id=new_id("ord-"),
            account_id=self.account_id,
            symbol=request.symbol.upper(),
            side=Side(request.side),
            qty=int(request.qty),
            type=OrderType(request.type),
            status=OrderStatus.WORKING,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            parent_id=parent_id,
            oco_group=request.oco_group,
            client_order_id=request.client_order_id,
        )
        self._orders[order.id] = order
        if request.client_order_id:
            self._client_ids[request.client_order_id] = order.id

        problem = self._validate(order)
        if problem is None and not ignore_failures and self.failures.reject_next > 0:
            self.failures.reject_next -= 1
            problem = self.failures.reject_reason
        if problem is not None:
            order.status = OrderStatus.REJECTED
            order.reject_reason = problem
            self._emit(BrokerEventType.ORDER_REJECTED, order=order, message=problem)
            return order

        if not ignore_failures and self.failures.partial_next > 0 and order.qty > 1:
            self.failures.partial_next -= 1
            self._partials[order.id] = _PartialPlan(
                self.failures.partial_ratio, self.failures.partial_remainder_delay
            )
        self._emit(BrokerEventType.ORDER_PLACED, order=order)
        self._try_execute(order, on_arrival=True)
        return order

    def _validate(self, order: Order) -> str | None:
        try:
            spec = get_spec(order.symbol)
        except UnknownSymbolError as exc:
            return str(exc)
        if order.qty <= 0:
            return "Quantity must be positive"
        if order.type is OrderType.LIMIT:
            if not order.limit_price or order.limit_price <= 0:
                return "Limit order requires limit_price"
            order.limit_price = round_to_tick(order.limit_price, spec.tick_size)
        if order.type is OrderType.STOP:
            if not order.stop_price or order.stop_price <= 0:
                return "Stop order requires stop_price"
            order.stop_price = round_to_tick(order.stop_price, spec.tick_size)
        for attr in ("stop_loss", "take_profit"):
            value = getattr(order, attr)
            if value is not None:
                setattr(order, attr, round_to_tick(value, spec.tick_size))
        return None

    def _on_price(self, underlying: str, price: float) -> None:
        active = [
            o for o in self._orders.values()
            if o.is_active and o.type is not OrderType.MARKET
            and get_spec(o.symbol).underlying == underlying
        ]
        for order in sorted(active, key=lambda o: o.created_at):
            if order.is_active:  # an earlier fill may have cancelled it (OCO)
                self._try_execute(order, on_arrival=False)

    def _try_execute(self, order: Order, *, on_arrival: bool) -> None:
        if not order.is_active or order.id in self._held:
            return
        market = self.market.price(order.symbol)
        fill_price: float | None = None
        if order.type is OrderType.MARKET:
            fill_price = market
        elif order.type is OrderType.LIMIT:
            limit = order.limit_price
            crossed = market <= limit if order.side is Side.BUY else market >= limit
            if crossed:
                fill_price = market if on_arrival else limit
        elif order.type is OrderType.STOP:
            stop = order.stop_price
            triggered = market >= stop if order.side is Side.BUY else market <= stop
            if triggered:
                fill_price = market
        if fill_price is None:
            return

        qty = order.remaining
        plan = self._partials.get(order.id)
        if plan is not None and not plan.done:
            plan.done = True
            qty = max(1, min(order.remaining - 1, math.floor(order.remaining * plan.ratio)))
            self._held.add(order.id)
            if plan.remainder_delay is not None:
                asyncio.get_running_loop().call_later(plan.remainder_delay, self._release, order.id)
        self._apply_fill(order, qty, fill_price)

    def _release(self, order_id: str) -> None:
        self._held.discard(order_id)
        order = self._orders.get(order_id)
        if order is not None and order.is_active:
            self._try_execute(order, on_arrival=True)

    def _apply_fill(self, order: Order, qty: int, price: float) -> None:
        fill = Fill(
            id=new_id("fill-"), order_id=order.id, account_id=self.account_id,
            symbol=order.symbol, side=order.side, qty=qty, price=price,
        )
        self._fills.append(fill)
        prev_filled = order.filled_qty
        order.filled_qty += qty
        order.avg_fill_price = round(
            ((order.avg_fill_price or 0.0) * prev_filled + price * qty) / order.filled_qty, 6
        )
        if order.filled_qty >= order.qty:
            order.status = OrderStatus.FILLED
            self._held.discard(order.id)
        order.updated_at = time.time()

        self._update_position(order.symbol, order.side, qty, price)
        self._emit(BrokerEventType.ORDER_FILLED, order=order, fill=fill)
        self._emit(
            BrokerEventType.POSITION_CHANGED,
            position=self._position(order.symbol, self._positions[order.symbol]),
        )
        self._clamp_brackets(order.symbol)
        if order.oco_group:
            self._handle_oco(order, qty)
        if order.stop_loss is not None or order.take_profit is not None:
            self._sync_bracket_children(order, qty)

    def _update_position(self, symbol: str, side: Side, qty: int, price: float) -> None:
        state = self._positions.setdefault(symbol, _PositionState())
        signed = side.sign * qty
        if state.qty == 0 or (state.qty > 0) == (signed > 0):
            new_qty = state.qty + signed
            state.avg_price = round(
                (state.avg_price * abs(state.qty) + price * qty) / abs(new_qty), 6
            )
            state.day_price = round(
                (state.day_price * abs(state.qty) + price * qty) / abs(new_qty), 6
            )
            state.qty = new_qty
            return
        closing = min(abs(state.qty), qty)
        direction = 1 if state.qty > 0 else -1
        realized = pnl(symbol, state.avg_price, price, direction * closing)  # since entry -> balance
        realized_day = pnl(symbol, state.day_price, price, direction * closing)  # since the day start
        state.realized_today = round(state.realized_today + realized_day, 2)
        self._realized_today = round(self._realized_today + realized_day, 2)
        self._realized_total = round(self._realized_total + realized, 2)
        state.qty += side.sign * closing
        if state.qty == 0:
            state.avg_price = 0.0
            state.day_price = 0.0
        opening = qty - closing
        if opening > 0:
            state.qty = side.sign * opening
            state.avg_price = price
            state.day_price = price

    def _position(self, symbol: str, state: _PositionState) -> Position:
        market = self.market.price(symbol)
        unrealized = pnl(symbol, state.avg_price, market, state.qty) if state.qty else 0.0
        return Position(
            account_id=self.account_id, symbol=symbol, qty=state.qty,
            avg_price=state.avg_price, market_price=market,
            unrealized_pnl=unrealized, realized_pnl=state.realized_today,
        )

    def _handle_oco(self, filled: Order, qty: int) -> None:
        siblings = [
            o for o in self._orders.values()
            if o.is_active and o.oco_group == filled.oco_group and o.id != filled.id
        ]
        for sibling in siblings:
            if filled.status is OrderStatus.FILLED:
                self._cancel(sibling, "OCO sibling filled")
            elif sibling.parent_id is not None:
                continue  # bracket exits are already sized to the position by _clamp_brackets
            elif sibling.remaining <= qty:
                self._cancel(sibling, "OCO sibling filled")
            else:
                sibling.qty -= qty
                sibling.updated_at = time.time()
                self._emit(BrokerEventType.ORDER_MODIFIED, order=sibling, message="OCO reduce")

    def _clamp_brackets(self, symbol: str) -> None:
        """Bracket exits are position-linked (as on most retail platforms): they
        shrink with the position and are cancelled once it is flat or reversed.
        Standalone stop/limit orders are left untouched."""
        position = self._positions[symbol].qty
        for child in list(self._orders.values()):
            if not child.is_active or child.parent_id is None or child.symbol != symbol:
                continue
            if position == 0 or child.side.sign * position > 0:
                self._cancel(child, "Bracket cancelled: position closed")
            elif child.remaining > abs(position):
                child.qty = child.filled_qty + abs(position)
                child.updated_at = time.time()
                self._emit(BrokerEventType.ORDER_MODIFIED, order=child,
                           message="Bracket reduced to position size")

    def _sync_bracket_children(self, parent: Order, fill_qty: int) -> None:
        children = [o for o in self._orders.values() if o.parent_id == parent.id]
        if not children:
            group = f"oco-{parent.id}"
            exit_side = parent.side.opposite
            if parent.stop_loss is not None:
                self._submit(
                    OrderRequest(parent.symbol, exit_side, parent.filled_qty, OrderType.STOP,
                                 stop_price=parent.stop_loss, oco_group=group),
                    ignore_failures=True, parent_id=parent.id,
                )
            if parent.take_profit is not None:
                self._submit(
                    OrderRequest(parent.symbol, exit_side, parent.filled_qty, OrderType.LIMIT,
                                 limit_price=parent.take_profit, oco_group=group),
                    ignore_failures=True, parent_id=parent.id,
                )
            return
        for child in children:
            if child.is_active:
                child.qty += fill_qty
                child.updated_at = time.time()
                self._emit(BrokerEventType.ORDER_MODIFIED, order=child, message="Bracket size follows entry fill")

    def _cancel(self, order: Order, reason: str) -> None:
        order.status = OrderStatus.CANCELLED
        order.updated_at = time.time()
        self._held.discard(order.id)
        self._emit(BrokerEventType.ORDER_CANCELLED, order=order, message=reason)
