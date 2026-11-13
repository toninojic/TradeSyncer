import asyncio

import pytest

from app.brokers.base import (
    BrokerConnectionError,
    BrokerEventType,
    NotConnectedError,
    OrderNotWorkingError,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from app.brokers.sim import SimMarket, SimulatedBroker
from app.brokers.tradovate import TradovateAdapter
from app.core.contracts import CONTRACTS, pnl, round_to_tick


@pytest.fixture
async def broker():
    market = SimMarket({"NASDAQ100": 25000.0, "SP500": 6800.0})
    b = SimulatedBroker("acc", market)
    await b.connect()
    return b


def drain(stream):
    events = []
    while stream.pending:
        events.append(stream._queue.get_nowait())
    return events


@pytest.mark.parametrize(
    ("symbol", "point_value"),
    [("NQ", 20), ("MNQ", 2), ("ES", 50), ("MES", 5), ("YM", 5), ("MYM", 0.5), ("RTY", 50), ("M2K", 5)],
)
def test_contract_specs(symbol, point_value):
    assert CONTRACTS[symbol].point_value == point_value
    assert pnl(symbol, 100, 101, 1) == point_value
    assert pnl(symbol, 100, 101, -2) == -2 * point_value


def test_round_to_tick():
    assert round_to_tick(25000.13, 0.25) == 25000.25
    assert round_to_tick(2450.04, 0.1) == 2450.0
    assert round_to_tick(47000.6, 1) == 47001


async def test_market_order_fills_and_updates_pnl(broker):
    order = await broker.place_order(OrderRequest("NQ", Side.BUY, 2))
    assert order.status is OrderStatus.FILLED and order.avg_fill_price == 25000.0
    broker.market.set_price("NQ", 25010)
    assert broker.account_snapshot().unrealized_pnl == 400
    await broker.place_order(OrderRequest("NQ", Side.SELL, 1))
    snap = broker.account_snapshot()
    assert snap.realized_pnl_today == 200 and snap.unrealized_pnl == 200
    assert snap.balance == 50_200


async def test_day_reset_carries_open_positions_at_the_reset_price(broker):
    await broker.place_order(OrderRequest("NQ", Side.BUY, 2))
    broker.market.set_price("NQ", 24990)  # -$400 open
    assert broker.account_snapshot().day_pnl == -400
    broker.reset_day()
    snap = broker.account_snapshot()
    assert snap.day_pnl == 0  # the new day starts at zero ...
    assert snap.unrealized_pnl == -400  # ... while the position still shows its P&L since entry
    assert broker.positions_snapshot()[0].avg_price == 25000  # entry price is kept
    broker.market.set_price("NQ", 24985)
    assert broker.account_snapshot().day_pnl == -200  # only the move since the reset
    await broker.place_order(OrderRequest("NQ", Side.SELL, 2))
    snap = broker.account_snapshot()
    assert snap.realized_pnl_today == -200 and snap.day_pnl == -200
    assert snap.balance == 50_000 - 600  # the balance carries the whole loss


async def test_micro_shares_price_with_mini(broker):
    await broker.place_order(OrderRequest("MNQ", Side.SELL, 10))
    broker.market.set_price("NQ", 24990)
    assert broker.account_snapshot().unrealized_pnl == 200  # 10 MNQ x 10 pt x $2


async def test_limit_and_stop_trigger(broker):
    limit = await broker.place_order(OrderRequest("NQ", Side.BUY, 1, OrderType.LIMIT, limit_price=24990))
    stop = await broker.place_order(OrderRequest("NQ", Side.BUY, 1, OrderType.STOP, stop_price=25020))
    assert limit.status is OrderStatus.WORKING and stop.status is OrderStatus.WORKING
    broker.market.set_price("NQ", 24985)
    orders = {o.id: o for o in broker.orders_snapshot(active_only=False)}
    assert orders[limit.id].status is OrderStatus.FILLED
    assert orders[limit.id].avg_fill_price == 24990  # resting limit fills at its price
    broker.market.set_price("NQ", 25021)
    orders = {o.id: o for o in broker.orders_snapshot(active_only=False)}
    assert orders[stop.id].avg_fill_price == 25021  # stop fills at market


async def test_bracket_creates_oco_children(broker):
    await broker.place_order(OrderRequest("NQ", Side.BUY, 2, stop_loss=24980, take_profit=25040))
    working = {o.type: o for o in broker.orders_snapshot()}
    assert working[OrderType.STOP].qty == 2 and working[OrderType.STOP].side is Side.SELL
    assert working[OrderType.LIMIT].limit_price == 25040
    broker.market.set_price("NQ", 25040)
    assert broker.orders_snapshot() == []  # TP filled, SL cancelled
    assert broker.positions_snapshot() == []


async def test_bracket_follows_position_size(broker):
    await broker.place_order(OrderRequest("NQ", Side.BUY, 3, stop_loss=24980, take_profit=25040))
    await broker.place_order(OrderRequest("NQ", Side.SELL, 2))
    assert sorted(o.qty for o in broker.orders_snapshot()) == [1, 1]
    await broker.place_order(OrderRequest("NQ", Side.SELL, 1))
    assert broker.orders_snapshot() == []  # flat: bracket cancelled


async def test_oco_partial_fill_reduces_sibling(broker):
    await broker.place_order(OrderRequest("NQ", Side.BUY, 4, stop_loss=24980, take_profit=25040))
    tp = next(o for o in broker.orders_snapshot() if o.type is OrderType.LIMIT)
    broker.partial_fill_order(tp.id, ratio=0.5)
    broker.market.set_price("NQ", 25040)
    sl = next(o for o in broker.orders_snapshot() if o.type is OrderType.STOP)
    assert sl.qty == 2


async def test_modify_and_cancel(broker):
    o = await broker.place_order(OrderRequest("NQ", Side.BUY, 1, OrderType.LIMIT, limit_price=24900))
    o = await broker.modify_order(o.id, limit_price=24950, qty=3)
    assert (o.qty, o.limit_price) == (3, 24950)
    await broker.cancel_order(o.id)
    with pytest.raises(OrderNotWorkingError):
        await broker.cancel_order(o.id)


async def test_flatten(broker):
    await broker.place_order(OrderRequest("NQ", Side.BUY, 2, stop_loss=24900))
    await broker.place_order(OrderRequest("ES", Side.SELL, 1))
    await broker.flatten()
    assert broker.positions_snapshot() == [] and broker.orders_snapshot() == []


async def test_reject_injection(broker):
    broker.reject_next_orders(1, "No margin")
    o = await broker.place_order(OrderRequest("NQ", Side.BUY, 1))
    assert o.status is OrderStatus.REJECTED and o.reject_reason == "No margin"
    o = await broker.place_order(OrderRequest("NQ", Side.BUY, 1))
    assert o.status is OrderStatus.FILLED


async def test_invalid_orders_are_rejected(broker):
    o = await broker.place_order(OrderRequest("XX", Side.BUY, 1))
    assert o.status is OrderStatus.REJECTED
    o = await broker.place_order(OrderRequest("NQ", Side.BUY, 1, OrderType.LIMIT))
    assert o.status is OrderStatus.REJECTED


async def test_partial_fill_injection(broker):
    broker.partial_fill_next_orders(1, ratio=0.5, remainder_delay=0.05)
    o = await broker.place_order(OrderRequest("NQ", Side.BUY, 4))
    assert o.filled_qty == 2 and o.status is OrderStatus.WORKING
    await asyncio.sleep(0.1)
    assert broker.positions_snapshot()[0].qty == 4


async def test_disconnect_drops_events_and_blocks_requests(broker):
    stream = broker.subscribe()
    broker.inject_disconnect()
    with pytest.raises(NotConnectedError):
        await broker.place_order(OrderRequest("NQ", Side.BUY, 1))
    with pytest.raises(BrokerConnectionError):
        await broker.connect()  # still forced down
    broker.restore_connection()
    await broker.connect()
    types = [e.type for e in drain(stream)]
    assert types.count(BrokerEventType.CONNECTION_STATUS) >= 3


async def test_latency_injection(broker):
    broker.set_latency(50)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await broker.place_order(OrderRequest("NQ", Side.BUY, 1))
    assert loop.time() - t0 >= 0.045


async def test_duplicate_event_injection(broker):
    stream = broker.subscribe()
    broker.duplicate_next_events(1)
    await broker.place_order(OrderRequest("NQ", Side.BUY, 1))
    ids = [e.event_id for e in drain(stream)]
    assert len(ids) == len(set(ids)) + 1


async def test_client_order_id_is_idempotent(broker):
    a = await broker.place_order(OrderRequest("NQ", Side.BUY, 1, client_order_id="x"))
    b = await broker.place_order(OrderRequest("NQ", Side.BUY, 1, client_order_id="x"))
    assert a.id == b.id
    assert broker.positions_snapshot()[0].qty == 1


async def test_random_walk_moves_price():
    market = SimMarket(tick_interval=0.01, volatility_ticks=3, seed=7)
    start = market.price("NQ")
    market.start()
    await asyncio.sleep(0.1)
    await market.stop()
    assert market.price("NQ") != start
    assert market.price("MNQ") == market.price("NQ")


async def test_tradovate_stub_makes_no_calls():
    adapter = TradovateAdapter("t", username="u", password="secret-pw")
    assert "secret-pw" not in repr(adapter)
    with pytest.raises(Exception, match="stub"):
        await adapter.connect()
    with pytest.raises(NotImplementedError):
        await adapter.place_order(OrderRequest("NQ", Side.BUY, 1))
