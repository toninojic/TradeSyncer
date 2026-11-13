import time

from app.brokers.base import Order, OrderStatus, OrderType, Position, Side
from app.engine.config import MismatchMode
from app.engine.connection import Backoff
from app.engine.reconciler import ReconcileAction, SyncStatus, decide
from app.engine.state import Deduper, LeaderBook, PositionBook, merge_order


def d(target, actual, pending=0, mode=MismatchMode.AUTO_CORRECT, attempts=0):
    return decide(target=target, actual=actual, pending=pending, mode=mode,
                  attempts=attempts, max_attempts=3)


def test_in_sync():
    assert d(2, 2).action is ReconcileAction.NONE
    assert d(2, 2).status is SyncStatus.IN_SYNC


def test_pending_orders_cover_the_gap():
    r = d(2, 0, pending=2)
    assert r.action is ReconcileAction.WAIT and r.status is SyncStatus.PENDING


def test_auto_correct_sends_signed_gap():
    assert d(2, 0).correction == 2
    assert d(0, 3).correction == -3
    assert d(-2, 1, pending=-1).correction == -2


def test_alert_only_never_corrects():
    r = d(2, 0, mode=MismatchMode.ALERT_ONLY)
    assert r.action is ReconcileAction.ALERT and r.correction == 2


def test_gives_up_after_max_attempts():
    r = d(2, 0, attempts=3)
    assert r.action is ReconcileAction.ALERT
    assert "gave up" in r.reason


def order(oid="o1", filled=0, qty=2, side=Side.BUY, status=OrderStatus.WORKING, updated=None):
    return Order(id=oid, account_id="a", symbol="NQ", side=side, qty=qty, type=OrderType.MARKET,
                 status=status, filled_qty=filled, updated_at=updated or time.time())


def test_position_book_counts_each_fill_once():
    book = PositionBook()
    assert book.apply_order(order(filled=1)) == 1
    assert book.apply_order(order(filled=1)) == 0  # duplicate
    assert book.apply_order(order(filled=2, status=OrderStatus.FILLED)) == 1
    assert book.apply_order(order(filled=1)) == 0  # stale snapshot
    assert book.position("NQ") == 2


def test_merge_never_regresses_terminal_status():
    filled = order(filled=2, status=OrderStatus.FILLED, updated=10)
    stale = order(filled=2, status=OrderStatus.WORKING, updated=11)
    assert merge_order(filled, stale).status is OrderStatus.FILLED


def test_load_initial_adopts_broker_positions():
    book = PositionBook()
    book.positions["ES"] = 3
    book.load_initial([order(filled=2, status=OrderStatus.FILLED)],
                      [Position("a", "NQ", 5)])
    assert book.positions == {"ES": 0, "NQ": 5}
    # a late event for an order already in the snapshot does not double count
    assert book.apply_order(order(filled=2, status=OrderStatus.FILLED)) == 0


def test_position_drift_adopted_only_when_confirmed():
    book = PositionBook()
    book.positions["NQ"] = 1
    assert book.verify_positions([Position("a", "NQ", 3)]) == []  # first sighting: wait
    assert book.verify_positions([Position("a", "NQ", 3)]) == [("NQ", 1, 3)]
    assert book.position("NQ") == 3


def test_transient_drift_is_forgotten():
    book = PositionBook()
    book.positions["NQ"] = 1
    book.verify_positions([Position("a", "NQ", 3)])
    book.verify_positions([Position("a", "NQ", 1)])  # back in line
    assert book.verify_positions([Position("a", "NQ", 3)]) == []


def test_leader_book_classifies_entries_and_exits():
    book = LeaderBook()
    book.positions["NQ"] = 2
    stop = Order("s", "a", "NQ", Side.SELL, 2, OrderType.STOP, OrderStatus.WORKING, stop_price=1)
    add = Order("l", "a", "NQ", Side.BUY, 1, OrderType.LIMIT, OrderStatus.WORKING, limit_price=1)
    reverse = Order("r", "a", "NQ", Side.SELL, 5, OrderType.LIMIT, OrderStatus.WORKING, limit_price=1)
    child = Order("c", "a", "NQ", Side.BUY, 2, OrderType.LIMIT, OrderStatus.WORKING,
                  limit_price=1, parent_id="p")
    assert book.classify(stop) == "exit"
    assert book.classify(add) == "entry"
    assert book.classify(reverse) == "entry"
    assert book.classify(child) == "exit"
    assert book.classify(order()) == "market"


def test_deduper_window():
    dd = Deduper(2)
    assert dd.is_new("a") and dd.is_new("b")
    assert not dd.is_new("a")
    assert dd.is_new("c")  # evicts "a"
    assert dd.is_new("a")


def test_backoff_grows_and_caps():
    b = Backoff(initial=0.5, maximum=4, jitter=0)
    assert [b.next_delay() for _ in range(6)] == [0.5, 1, 2, 4, 4, 4]
    b.reset()
    assert b.next_delay() == 0.5
