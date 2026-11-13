"""Failure handling: partial fills, rejections, disconnects, duplicates, latency."""

import asyncio

import pytest

from app.brokers.base import OrderRequest, OrderType, Side
from app.engine.events import FollowerAction, LogCategory, RecordStatus

from tests.conftest import NQ_PRICE, alert_only, follower_configs, make_rig

pytestmark = pytest.mark.integration


async def wait_until(predicate, rig, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await rig.settle()
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


# ------------------------------------------------------------------ partial fills


async def test_leader_partial_fill_is_copied_proportionally(rig):
    rig.leader.partial_fill_next_orders(1, ratio=0.5, remainder_delay=None)
    await rig.buy(4)
    assert rig.positions() == (2, 2, 1, 20)  # followers track the 2 that filled
    rig.leader.release_partial_fills()
    await rig.settle()
    assert rig.positions() == (4, 4, 2, 40)


async def test_follower_partial_fill_remainder_fills_later(rig):
    rig.f1.partial_fill_next_orders(1, ratio=0.5, remainder_delay=0.1)
    await rig.buy(4)
    assert rig.pos(rig.f1, "NQ") == 2  # remainder still working
    await rig.reconcile()  # pending remainder is not double-ordered
    assert rig.pos(rig.f1, "NQ") == 2
    await wait_until(lambda: rig.pos(rig.f1, "NQ") == 4, rig)
    await rig.reconcile()
    assert rig.positions() == (4, 4, 2, 40)
    reconciles = [r for r in rig.sink.orders_for("f1") if r.action is FollowerAction.RECONCILE]
    assert reconciles == []


async def test_follower_partial_fill_stuck_remainder_is_replaced(rig):
    rig.f1.partial_fill_next_orders(1, ratio=0.5, remainder_delay=None)  # never fills by itself
    await rig.buy(4)
    assert rig.pos(rig.f1, "NQ") == 2
    await asyncio.sleep(0.6)  # older than stale_market_order_s
    await rig.reconcile()
    assert rig.pos(rig.f1, "NQ") == 4
    assert rig.working(rig.f1) == []


async def test_leader_partial_limit_fill_does_not_churn_follower(rig):
    # The leader's resting limit will fill only 2 of 4 when the price touches it.
    rig.leader.partial_fill_next_orders(1, ratio=0.5, remainder_delay=None)
    await rig.buy(4, type=OrderType.LIMIT, limit_price=NQ_PRICE - 10)
    rig.market.set_price("NQ", NQ_PRICE - 10)
    await rig.settle()
    await rig.reconcile()
    # Followers' own limits filled fully; they are ahead of the leader but must not
    # sell back while the leader's order is still working.
    assert rig.positions() == (2, 4, 2, 40)
    rig.leader.release_partial_fills()
    await rig.settle()
    await rig.reconcile()
    assert rig.positions() == (4, 4, 2, 40)
    sells = [r for r in rig.sink.follower_orders.values() if r.side == "sell"]
    assert sells == []


# ------------------------------------------------------------------ rejections


async def test_rejected_follower_order_is_retried_by_reconciliation(rig):
    rig.f1.reject_next_orders(1, "Insufficient margin")
    await rig.buy(2)
    assert rig.positions() == (2, 0, 1, 20)
    [rejected] = [r for r in rig.sink.orders_for("f1") if r.status is RecordStatus.REJECTED]
    assert rejected.error == "Insufficient margin"
    await rig.reconcile()
    assert rig.positions() == (2, 2, 1, 20)


async def test_reconciliation_gives_up_after_max_attempts():
    rig = await make_rig()
    try:
        rig.f1.reject_next_orders(10, "Account blocked")
        await rig.buy(2)
        for _ in range(5):
            await rig.reconcile()
        attempts = [r for r in rig.sink.orders_for("f1") if r.action is FollowerAction.RECONCILE]
        assert len(attempts) == rig.engine.settings.max_correction_attempts
        assert rig.engine.workers["f1"].sync_status == "out_of_sync"
        alerts = [l for l in rig.sink.logs_in(LogCategory.RECONCILE) if "gave up" in l.message]
        assert len(alerts) == 1  # alerted once, not on every tick
        # after fixing the account, a manual "retry sync" resets the counter
        rig.f1.clear_failures()
        await rig.engine.retry_sync("f1")
        await rig.settle()
        assert rig.positions() == (2, 2, 1, 20)
    finally:
        await rig.engine.stop()


async def test_alert_only_follower_is_not_corrected():
    rig = await make_rig(configs=alert_only(follower_configs(), "f1"))
    try:
        rig.f1.reject_next_orders(1)
        await rig.buy(2)
        await rig.reconcile()
        await rig.reconcile()
        assert rig.pos(rig.f1, "NQ") == 0
        assert rig.engine.workers["f1"].sync_status == "out_of_sync"
        assert [r for r in rig.sink.orders_for("f1") if r.action is FollowerAction.RECONCILE] == []
        assert len([l for l in rig.sink.logs_in(LogCategory.RECONCILE) if l.account_id == "f1"]) == 1
        # the next leader fill is still copied (as a delta, not a correction)
        await rig.buy(1)
        assert rig.pos(rig.f1, "NQ") == 1
    finally:
        await rig.engine.stop()


# ------------------------------------------------------------------ disconnects


async def test_follower_disconnect_mid_trade_reconnects_and_resyncs(rig):
    await rig.buy(2)
    rig.f1.inject_disconnect(duration_s=0.3)
    await rig.settle()
    await rig.sell(2)  # leader exits while f1 is offline
    assert rig.positions() == (0, 2, 0, 0)
    failed = [r for r in rig.sink.orders_for("f1") if r.status is RecordStatus.FAILED]
    assert failed and "not connected" in failed[-1].error
    # reconnect with backoff, then reconciliation closes the stale position
    await wait_until(lambda: rig.f1.is_connected, rig, timeout=5)
    await rig.reconcile()
    assert rig.positions() == (0, 0, 0, 0)
    conn = [l.message for l in rig.sink.logs_in(LogCategory.CONNECTION) if l.account_id == "f1"]
    assert any("disconnected" in m for m in conn) and any(m == "Follower connected" for m in conn)


async def test_leader_disconnect_fills_while_offline_are_caught_up(rig):
    await rig.buy(2, stop_loss=NQ_PRICE - 20)
    rig.leader.inject_disconnect(duration_s=0.2)
    rig.market.set_price("NQ", NQ_PRICE - 25)  # stops trigger everywhere; leader events are lost
    await rig.settle()
    await wait_until(lambda: rig.leader.is_connected, rig, timeout=5)
    await rig.reconcile()
    assert rig.positions() == (0, 0, 0, 0)
    for broker in rig.followers:
        assert rig.working(broker) == []


# ------------------------------------------------------------------ duplicates & pending


async def test_duplicate_leader_event_is_ignored(rig):
    rig.leader.duplicate_next_events(10)
    await rig.buy(2)
    assert rig.positions() == (2, 2, 1, 20)
    copies = [r for r in rig.sink.orders_for("f1") if r.action is FollowerAction.COPY_FILL]
    assert len(copies) == 1
    assert any("Duplicate" in l.message for l in rig.sink.logs)


async def test_leader_closes_while_follower_order_pending(rig):
    rig.f1.set_latency(300)  # f1 acks slowly
    await rig.leader.place_order(OrderRequest("NQ", Side.BUY, 2))
    await rig.leader.place_order(OrderRequest("NQ", Side.SELL, 2))  # close immediately
    await rig.settle()
    await rig.reconcile()
    assert rig.positions() == (0, 0, 0, 0)
    # f1 never went short
    sides = [(r.side, r.qty) for r in rig.sink.orders_for("f1") if r.action is FollowerAction.COPY_FILL]
    assert sum(q if s == "buy" else -q for s, q in sides) == 0


async def test_leader_closes_while_mirrored_limit_still_unfilled(rig):
    """f1's mirrored limit fills only partly when the leader's fills, then the
    leader closes: the unfilled remainder is cancelled and f1 never reverses."""
    rig.f1.partial_fill_next_orders(1, ratio=0.5, remainder_delay=None)
    await rig.buy(2, type=OrderType.LIMIT, limit_price=NQ_PRICE - 10)
    rig.market.set_price("NQ", NQ_PRICE - 10)
    await rig.settle()
    assert rig.positions() == (2, 2 - 1, 1, 20)  # f1: 1 filled, 1 still working
    await rig.reconcile()  # within the grace period nothing is "corrected"
    assert rig.pos(rig.f1, "NQ") == 1

    await rig.sell(2)
    await rig.reconcile()
    assert rig.positions() == (0, 0, 0, 0)
    assert rig.working(rig.f1) == []
    sells = sum(r.qty for r in rig.sink.orders_for("f1") if r.side == "sell" and r.action in (
        FollowerAction.COPY_FILL, FollowerAction.RECONCILE))
    assert sells == 1


async def test_fast_leader_events_do_not_churn_follower(rig):
    """Leader fill and close arrive before f1 processes the first fill: f1 must
    not trade the stale intermediate state (no temporary reversal)."""
    rig.f1.partial_fill_next_orders(1, ratio=0.5, remainder_delay=None)
    await rig.buy(2, type=OrderType.LIMIT, limit_price=NQ_PRICE - 10)
    rig.market.set_price("NQ", NQ_PRICE - 10)  # no settle: the close follows immediately
    await rig.leader.place_order(OrderRequest("NQ", Side.SELL, 2))
    await rig.settle()
    await rig.reconcile()
    assert rig.positions() == (0, 0, 0, 0)
    traded = [(r.side, r.qty) for r in rig.sink.orders_for("f1")
              if r.action in (FollowerAction.COPY_FILL, FollowerAction.RECONCILE)]
    assert traded == [("sell", 1)]


async def test_trade_log_records_skipped_copies_while_offline(rig):
    rig.f2.inject_disconnect()
    await rig.buy(2)
    failed = [r for r in rig.sink.orders_for("f2") if r.status is RecordStatus.FAILED]
    assert failed and "not connected" in failed[0].error
    rig.f2.restore_connection()
    await wait_until(lambda: rig.f2.is_connected, rig, timeout=5)
    await rig.reconcile()
    assert rig.positions() == (2, 2, 1, 20)


async def test_order_timeout_marks_failure_and_reconciles():
    from app.engine.config import EngineSettings

    rig = await make_rig(settings=EngineSettings(
        reconcile_interval_s=3600, order_timeout_s=0.1, linked_fill_grace_s=0.3,
        stale_market_order_s=0.5,
    ))
    try:
        rig.f1.set_latency(250)
        await rig.leader.place_order(OrderRequest("NQ", Side.BUY, 1))
        await asyncio.sleep(0.15)  # the f1 copy has timed out by now
        rig.f1.set_latency(0)
        await rig.settle()
        timeouts = [r for r in rig.sink.orders_for("f1") if r.error and "Timed out" in r.error]
        assert len(timeouts) == 1
        # the timeout triggers a resync; reconciliation then fixes the position
        assert rig.positions() == (1, 1, 0, 10)
    finally:
        await rig.engine.stop()
