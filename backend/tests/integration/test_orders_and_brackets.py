"""Resting entry orders and bracket SL/TP: place, modify, cancel, trigger."""

import pytest

from app.brokers.base import OrderRequest, OrderType, Side

from tests.conftest import NQ_PRICE

pytestmark = pytest.mark.integration


async def test_limit_entry_is_mirrored_and_fills_together(rig):
    await rig.buy(2, type=OrderType.LIMIT, limit_price=NQ_PRICE - 10)
    for broker, symbol, qty in ((rig.f1, "NQ", 2), (rig.f2, "NQ", 1), (rig.f3, "MNQ", 20)):
        [order] = rig.working(broker, OrderType.LIMIT)
        assert (order.symbol, order.qty, order.limit_price) == (symbol, qty, NQ_PRICE - 10)

    rig.market.set_price("NQ", NQ_PRICE - 10)
    await rig.settle()
    assert rig.positions() == (2, 2, 1, 20)
    assert all(not rig.working(b) for b in rig.followers)


async def test_limit_entry_modify_and_cancel(rig):
    await rig.buy(2, type=OrderType.LIMIT, limit_price=NQ_PRICE - 10)
    [leader_order] = rig.working(rig.leader)

    await rig.leader.modify_order(leader_order.id, limit_price=NQ_PRICE - 20, qty=4)
    await rig.settle()
    assert [(o.qty, o.limit_price) for o in rig.working(rig.f1)] == [(4, NQ_PRICE - 20)]
    assert [(o.qty, o.limit_price) for o in rig.working(rig.f2)] == [(2, NQ_PRICE - 20)]
    assert [(o.qty, o.limit_price) for o in rig.working(rig.f3)] == [(40, NQ_PRICE - 20)]

    await rig.leader.cancel_order(leader_order.id)
    await rig.settle()
    assert all(not rig.working(b) for b in rig.followers)


async def test_bracket_sl_tp_mirrored_with_follower_sizes(rig):
    await rig.buy(2, stop_loss=NQ_PRICE - 20, take_profit=NQ_PRICE + 40)
    assert rig.positions() == (2, 2, 1, 20)
    for broker, qty in ((rig.f1, 2), (rig.f2, 1), (rig.f3, 20)):
        orders = {o.type: o for o in rig.working(broker)}
        assert orders[OrderType.STOP].stop_price == NQ_PRICE - 20
        assert orders[OrderType.STOP].qty == qty
        assert orders[OrderType.LIMIT].limit_price == NQ_PRICE + 40
        assert orders[OrderType.LIMIT].qty == qty
        assert orders[OrderType.STOP].side is Side.SELL


async def test_modify_stop_loss_and_cancel_take_profit(rig):
    await rig.buy(2, stop_loss=NQ_PRICE - 20, take_profit=NQ_PRICE + 40)
    leader_orders = {o.type: o for o in rig.working(rig.leader)}

    await rig.leader.modify_order(leader_orders[OrderType.STOP].id, stop_price=NQ_PRICE - 5)
    await rig.settle()
    for broker in rig.followers:
        [stop] = rig.working(broker, OrderType.STOP)
        assert stop.stop_price == NQ_PRICE - 5

    await rig.leader.cancel_order(leader_orders[OrderType.LIMIT].id)
    await rig.settle()
    for broker in rig.followers:
        assert rig.working(broker, OrderType.LIMIT) == []
        assert len(rig.working(broker, OrderType.STOP)) == 1


async def test_stop_loss_hit_closes_everyone_and_cancels_tp(rig):
    await rig.buy(2, stop_loss=NQ_PRICE - 20, take_profit=NQ_PRICE + 40)
    rig.market.set_price("NQ", NQ_PRICE - 25)
    await rig.settle()
    await rig.reconcile()
    assert rig.positions() == (0, 0, 0, 0)
    for broker in [rig.leader, *rig.followers]:
        assert rig.working(broker) == []
    # nobody got reversed into a short by a double exit
    for broker in rig.followers:
        assert all(p.qty >= 0 for p in broker.positions_snapshot())


async def test_take_profit_hit(rig):
    await rig.buy(2, stop_loss=NQ_PRICE - 20, take_profit=NQ_PRICE + 40)
    rig.market.set_price("NQ", NQ_PRICE + 40)
    await rig.settle()
    assert rig.positions() == (0, 0, 0, 0)
    assert rig.f3.account_snapshot().realized_pnl_today == 1600.0  # 20 MNQ x 40 x $2


async def test_partial_close_shrinks_bracket_everywhere(rig):
    await rig.buy(4, stop_loss=NQ_PRICE - 20, take_profit=NQ_PRICE + 40)
    assert rig.positions() == (4, 4, 2, 40)
    await rig.sell(2)  # partial close at market; the sim shrinks the leader's bracket
    assert rig.positions() == (2, 2, 1, 20)
    for broker, pos in ((rig.leader, 2), (rig.f1, 2), (rig.f2, 1), (rig.f3, 20)):
        assert sorted(o.qty for o in rig.working(broker)) == [pos, pos]
    rig.market.set_price("NQ", NQ_PRICE + 40)
    await rig.settle()
    assert rig.positions() == (0, 0, 0, 0)


async def test_follower_stop_never_exceeds_follower_position(rig):
    await rig.buy(4)
    # a standalone leader stop is not position-linked: it stays at 4 after a partial close
    await rig.leader.place_order(OrderRequest("NQ", Side.SELL, 4, OrderType.STOP, stop_price=NQ_PRICE - 20))
    await rig.sell(2)
    assert [o.qty for o in rig.working(rig.leader, OrderType.STOP)] == [4]
    assert rig.positions() == (2, 2, 1, 20)
    for broker, pos in ((rig.f1, 2), (rig.f2, 1), (rig.f3, 20)):
        [stop] = rig.working(broker, OrderType.STOP)
        assert stop.qty == pos


async def test_standalone_stop_added_later_is_mirrored(rig):
    await rig.buy(2)
    await rig.leader.place_order(
        OrderRequest("NQ", Side.SELL, 2, OrderType.STOP, stop_price=NQ_PRICE - 30)
    )
    await rig.settle()
    assert [o.qty for o in rig.working(rig.f2, OrderType.STOP)] == [1]
    assert [o.qty for o in rig.working(rig.f3, OrderType.STOP)] == [20]


async def test_copy_sl_tp_off(rig):
    cfg = rig.engine.workers["f1"].cfg
    cfg.copy_sl_tp = False
    await rig.buy(2, stop_loss=NQ_PRICE - 20, take_profit=NQ_PRICE + 40)
    assert rig.working(rig.f1) == []
    assert len(rig.working(rig.f2)) == 2
    # the exit is still copied when the leader's stop fills
    rig.market.set_price("NQ", NQ_PRICE - 25)
    await rig.settle()
    assert rig.positions() == (0, 0, 0, 0)


async def test_scale_out_with_two_take_profits(rig):
    await rig.buy(3)
    await rig.leader.place_order(OrderRequest("NQ", Side.SELL, 1, OrderType.LIMIT, limit_price=NQ_PRICE + 10))
    await rig.leader.place_order(OrderRequest("NQ", Side.SELL, 2, OrderType.LIMIT, limit_price=NQ_PRICE + 20))
    await rig.settle()
    rig.market.set_price("NQ", NQ_PRICE + 10)
    await rig.settle()
    assert rig.positions() == (2, 2, 1, 20)
    rig.market.set_price("NQ", NQ_PRICE + 20)
    await rig.settle()
    await rig.reconcile()
    assert rig.positions() == (0, 0, 0, 0)
    for broker in rig.followers:
        assert rig.working(broker) == []
