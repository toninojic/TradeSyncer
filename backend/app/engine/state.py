"""Position and order bookkeeping that is safe against duplicate and stale updates.

Positions are derived from *cumulative* filled quantities per order: an order
update only changes the position by ``filled_qty - already_applied``. The
same fill can therefore arrive via the ``place_order`` response, the event
stream and a REST snapshot without ever being counted twice.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from app.brokers.base import TERMINAL_STATUSES, Order, OrderType, Position


def merge_order(old: Order | None, new: Order) -> Order:
    """Return the newer of two snapshots of the same order."""
    if old is None:
        return new
    if new.filled_qty < old.filled_qty:
        return old
    if old.status in TERMINAL_STATUSES and new.status not in TERMINAL_STATUSES:
        return old
    if new.updated_at < old.updated_at and new.filled_qty == old.filled_qty:
        return old
    return new


class PositionBook:
    def __init__(self) -> None:
        self.positions: dict[str, int] = {}
        self.orders: dict[str, Order] = {}
        self.applied: dict[str, int] = {}
        self._drift: dict[str, int] = {}

    def position(self, symbol: str) -> int:
        return self.positions.get(symbol, 0)

    def is_flat(self) -> bool:
        return all(q == 0 for q in self.positions.values())

    def nonzero_positions(self) -> dict[str, int]:
        return {s: q for s, q in self.positions.items() if q}

    def apply_order(self, order: Order) -> int:
        """Merge an order snapshot; returns the signed position change it caused."""
        merged = merge_order(self.orders.get(order.id), order)
        self.orders[order.id] = merged
        already = self.applied.get(order.id, 0)
        if merged.filled_qty <= already:
            return 0
        delta = (merged.filled_qty - already) * merged.side.sign
        self.applied[order.id] = merged.filled_qty
        self.positions[merged.symbol] = self.positions.get(merged.symbol, 0) + delta
        return delta

    def load_initial(self, orders: Iterable[Order], positions: Iterable[Position]) -> None:
        """Adopt a broker snapshot as the starting point (startup)."""
        for order in orders:
            self.orders[order.id] = merge_order(self.orders.get(order.id), order)
            self.applied[order.id] = max(self.applied.get(order.id, 0), order.filled_qty)
        self.positions = {s: 0 for s in self.positions}
        for pos in positions:
            self.positions[pos.symbol] = pos.qty
        self._drift.clear()

    def apply_snapshot_orders(self, orders: Iterable[Order]) -> int:
        """Treat a REST order snapshot like events (fills missed while disconnected)."""
        changed = 0
        for order in orders:
            if self.apply_order(order):
                changed += 1
        return changed

    def verify_positions(self, broker_positions: Iterable[Position]) -> list[tuple[str, int, int]]:
        """Compare with the broker's positions. A difference seen on two
        consecutive checks is adopted (returns (symbol, local, broker))."""
        broker = {p.symbol: p.qty for p in broker_positions}
        adopted: list[tuple[str, int, int]] = []
        for symbol in set(broker) | set(self.positions):
            b = broker.get(symbol, 0)
            local = self.positions.get(symbol, 0)
            if b == local:
                self._drift.pop(symbol, None)
                continue
            if self._drift.get(symbol) == b:
                self.positions[symbol] = b
                self._drift.pop(symbol, None)
                adopted.append((symbol, local, b))
            else:
                self._drift[symbol] = b
        return adopted

    def active_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.is_active]


class LeaderBook(PositionBook):
    """Leader state plus a classification of each leader order as entry/exit."""

    def __init__(self) -> None:
        super().__init__()
        self.kinds: dict[str, str] = {}

    def classify(self, order: Order) -> str:
        if order.id in self.kinds:
            return self.kinds[order.id]
        if order.type is OrderType.MARKET:
            kind = "market"
        elif order.parent_id is not None:
            kind = "exit"
        else:
            pos = self.position(order.symbol)
            reduces = pos != 0 and (pos > 0) != (order.side.sign > 0)
            kind = "exit" if reduces and order.qty <= abs(pos) else "entry"
        self.kinds[order.id] = kind
        return kind


class Deduper:
    """Remembers the last N event ids."""

    def __init__(self, size: int) -> None:
        self._seen: set[str] = set()
        self._order: deque[str] = deque()
        self._size = size

    def is_new(self, event_id: str) -> bool:
        if event_id in self._seen:
            return False
        self._seen.add(event_id)
        self._order.append(event_id)
        if len(self._order) > self._size:
            self._seen.discard(self._order.popleft())
        return True
