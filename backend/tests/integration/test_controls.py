"""Pause/resume, kill switch, per-account flatten, joining, config changes, risk hook."""

import pytest

from app.brokers.base import OrderRequest, Side
from app.brokers.sim import SimMarket, SimulatedBroker
from app.engine.config import EngineSettings, FollowerConfig
from app.engine.copy_engine import CopyEngine
from app.engine.events import FollowerAction, LogCategory, MemorySink, RecordStatus

from tests.conftest import NQ_PRICE, _wait_connected, make_rig

pytestmark = pytest.mark.integration


async def test_paused_copier_ignores_leader_and_rejoins_on_next_trade(rig):
    await rig.engine.pause()
    await rig.buy(2)
    assert rig.positions() == (2, 0, 0, 0)
    assert rig.sink.leader_events[-1].copied is False

    await rig.engine.resume()
    await rig.settle()
    # the leader is already in a trade: followers wait instead of jumping in late
    assert rig.positions() == (2, 0, 0, 0)
    assert {f["sync_status"] for f in rig.engine.snapshot()["followers"]} == {"waiting_for_leader_flat"}

    await rig.sell(2)
    await rig.buy(1)  # next trade is copied
    assert rig.positions() == (1, 1, 0, 10)


async def test_resume_without_changes_continues_copying(rig):
    await rig.buy(2)
    await rig.engine.pause()
    await rig.engine.resume()
    await rig.settle()
    await rig.sell(2)
    assert rig.positions() == (0, 0, 0, 0)


async def test_kill_switch_flattens_all_followers_and_stops(rig):
    await rig.buy(2, stop_loss=NQ_PRICE - 20)
    await rig.engine.kill_switch()
    await rig.settle()
    assert rig.positions() == (2, 0, 0, 0)  # the leader is not touched
    for broker in rig.followers:
        assert rig.working(broker) == []
    assert rig.engine.running is False and rig.engine.killed is True
    assert rig.sink.logs_in(LogCategory.KILL_SWITCH)

    await rig.buy(1)  # nothing is copied while killed
    await rig.reconcile()
    assert rig.positions() == (3, 0, 0, 0)

    await rig.engine.resume()
    await rig.settle()
    assert rig.positions() == (3, 0, 0, 0)  # waits for the leader to be flat
    await rig.sell(3)
    await rig.buy(2)
    assert rig.positions() == (2, 2, 1, 20)


async def test_kill_switch_flattens_offline_follower_after_reconnect(rig):
    import asyncio

    await rig.buy(2)
    rig.f1.inject_disconnect()
    await rig.settle()
    await rig.engine.kill_switch()
    await rig.settle()
    assert rig.pos(rig.f1, "NQ") == 2  # could not be flattened while offline
    assert rig.engine.workers["f1"].flatten_pending
    rig.f1.restore_connection()
    for _ in range(200):
        await asyncio.sleep(0.02)
        if rig.f1.is_connected:
            break
    await rig.settle()
    assert rig.positions() == (2, 0, 0, 0)


async def test_flatten_single_follower(rig):
    await rig.buy(2)
    await rig.engine.flatten_follower("f2")
    await rig.settle()
    await rig.reconcile()
    assert rig.positions() == (2, 2, 0, 20)  # f2 stays flat, others keep copying
    await rig.sell(2)
    assert rig.positions() == (0, 0, 0, 0)
    await rig.buy(2)
    assert rig.positions() == (2, 2, 1, 20)  # f2 joins the next trade
    flattens = [r for r in rig.sink.orders_for("f2") if r.action is FollowerAction.FLATTEN]
    assert flattens and flattens[0].status is RecordStatus.OK


async def test_engine_start_with_leader_in_position_waits_for_flat():
    market = SimMarket({"NASDAQ100": NQ_PRICE})
    leader = SimulatedBroker("leader", market)
    follower = SimulatedBroker("f1", market)
    await leader.connect()
    await leader.place_order(OrderRequest("NQ", Side.BUY, 2))
    await leader.disconnect()
    engine = CopyEngine(leader, [(follower, FollowerConfig("f1"))], sink=MemorySink(),
                        settings=EngineSettings(reconcile_interval_s=3600))
    await engine.start(reconcile_loop=False)
    try:
        class R:  # minimal rig for _wait_connected
            pass
        r = R()
        r.engine = engine
        await _wait_connected(r)
        await engine.wait_idle()
        await engine.reconcile_now()
        await engine.wait_idle()
        assert follower.positions_snapshot() == []
        assert engine.workers["f1"].sync_status == "waiting_for_leader_flat"
    finally:
        await engine.stop()


async def test_disabled_follower_is_not_copied(rig):
    cfg = rig.engine.workers["f3"].cfg
    cfg.copy_enabled = False
    rig.engine.update_follower(cfg)
    await rig.buy(2)
    assert rig.positions() == (2, 2, 1, 0)


async def test_multiplier_change_applies_on_next_reconcile(rig):
    await rig.buy(4)
    cfg = rig.engine.workers["f2"].cfg
    cfg.multiplier = 0.25
    rig.engine.update_follower(cfg)
    await rig.reconcile()
    assert rig.pos(rig.f2, "NQ") == 1


async def test_risk_guard_can_block_orders():
    from app.engine.risk import AllowAll

    class MaxTwo(AllowAll):
        def check_order(self, follower_id, request, current_position, reduces_exposure):
            if not reduces_exposure and abs(current_position + request.side.sign * request.qty) > 2:
                return "max position 2"
            return None

    rig = await make_rig()
    try:
        rig.engine.risk = MaxTwo()
        await rig.buy(2)
        assert rig.positions() == (2, 2, 1, 0)  # f3 would need 20 MNQ: blocked
        blocked = [r for r in rig.sink.orders_for("f3") if r.status is RecordStatus.BLOCKED]
        assert blocked and blocked[0].error == "max position 2"
        assert rig.sink.logs_in(LogCategory.RISK)
    finally:
        await rig.engine.stop()


async def test_reconcile_loop_runs_automatically():
    rig = await make_rig(reconcile_loop=True)
    try:
        rig.f1.reject_next_orders(1)
        await rig.leader.place_order(OrderRequest("NQ", Side.BUY, 2))
        import asyncio

        for _ in range(100):
            await asyncio.sleep(0.02)
            if rig.pos(rig.f1, "NQ") == 2:
                break
        assert rig.positions() == (2, 2, 1, 20)
    finally:
        await rig.engine.stop()
