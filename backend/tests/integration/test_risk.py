"""Risk rules end to end: daily loss limit, max position, trading hours."""

from datetime import datetime, timedelta, timezone

import pytest

from app.brokers.base import OrderRequest, OrderType, Side
from app.engine.config import MismatchMode
from app.engine.events import FollowerAction, LogCategory, RecordStatus
from app.engine.risk import MemoryRiskStore, RiskManager, RiskRulesCfg

from tests.conftest import NQ_PRICE, follower_configs, make_rig

pytestmark = pytest.mark.integration

WED_10_ET = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)  # inside 09:30-16:00 ET
WED_08_ET = datetime(2026, 11, 11, 13, 0, tzinfo=timezone.utc)  # before the window
WED_17_ET = datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc)  # after the window


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def risk_rig(rules: dict[str, RiskRulesCfg], now: datetime = WED_10_ET, alert_only=()):
    clock = Clock(now)
    store = MemoryRiskStore()
    risk = RiskManager(store=store, now_fn=clock, check_interval_s=3600)
    risk.set_rules(rules)
    configs = follower_configs()
    for cfg in configs:
        if cfg.account_id in rules:
            cfg.risk_max_position = rules[cfg.account_id].max_position
        if cfg.account_id in alert_only:
            cfg.mismatch_mode = MismatchMode.ALERT_ONLY
    rig = await make_rig(configs=configs, risk=risk)
    await risk.start()
    return rig, risk, clock, store


async def check(rig, risk) -> None:
    """One risk evaluation plus a reconciliation tick, fully settled."""
    await rig.settle()
    await risk.evaluate()
    await rig.settle()
    await rig.reconcile()


async def close(rig, risk) -> None:
    await risk.stop()
    await rig.engine.stop()


def status(rig, follower_id: str) -> str:
    return str(rig.engine.workers[follower_id].sync_status)


# ------------------------------------------------------------------ daily loss limit


async def test_daily_loss_limit_flattens_and_locks_the_follower():
    rig, risk, _, store = await risk_rig({"f1": RiskRulesCfg(daily_loss_limit=300)})
    try:
        await rig.buy(2, stop_loss=NQ_PRICE - 50)
        assert rig.positions() == (2, 2, 1, 20)

        rig.market.move("NQ", -7)  # f1: 2 x 7 x $20 = -280, still inside the limit
        await check(rig, risk)
        assert rig.pos(rig.f1, "NQ") == 2 and not risk.is_locked("f1")

        rig.market.move("NQ", -3)  # -400 unrealized: breach (realized + unrealized)
        await check(rig, risk)
        assert rig.positions() == (2, 0, 1, 20)  # only f1 is flattened
        assert rig.working(rig.f1) == []  # its SL is gone too
        assert risk.is_locked("f1") and store.states["f1"].locked
        assert status(rig, "f1") == "risk_locked"

        [event] = rig.sink.risk_events
        assert (event.follower_id, event.rule, event.trading_day) == ("f1", "daily_loss_limit", "2026-11-11")
        assert "Daily loss limit $300.00 reached" in event.message
        assert any("copying is disabled" in l.message for l in rig.sink.logs_in(LogCategory.RISK))

        # nothing is copied to f1 while locked; the others carry on
        await rig.sell(2)
        await rig.buy(1)
        await check(rig, risk)
        assert rig.positions() == (1, 0, 0, 10)
        assert len(rig.sink.risk_events) == 1  # no repeated breach events
    finally:
        await close(rig, risk)


async def test_manual_re_enable_gives_a_fresh_allowance():
    rig, risk, _, _ = await risk_rig({"f1": RiskRulesCfg(daily_loss_limit=300)})
    try:
        await rig.buy(2)
        rig.market.move("NQ", -10)  # -400
        await check(rig, risk)
        assert risk.is_locked("f1")
        assert await risk.unlock("f1") is True
        assert await risk.unlock("f1") is False  # already unlocked
        await check(rig, risk)
        # the leader is still in the trade: f1 does not jump in
        assert rig.pos(rig.f1, "NQ") == 0 and status(rig, "f1") == "waiting_for_leader_flat"

        await rig.sell(2)
        await rig.buy(1)
        assert rig.pos(rig.f1, "NQ") == 1
        assert risk.snapshot("f1")["loss_used"] == 0.0  # measured from -400 now

        rig.market.move("NQ", -10)  # another -200: inside the fresh $300
        await check(rig, risk)
        assert rig.pos(rig.f1, "NQ") == 1 and risk.snapshot("f1")["loss_used"] == 200.0
        rig.market.move("NQ", -6)  # -320 since the re-enable: second breach
        await check(rig, risk)
        assert rig.pos(rig.f1, "NQ") == 0 and risk.is_locked("f1")
        assert [e.rule for e in rig.sink.risk_events] == ["daily_loss_limit"] * 2
    finally:
        await close(rig, risk)


async def test_new_trading_day_unlocks_and_resets_day_pnl():
    rig, risk, clock, store = await risk_rig({"f1": RiskRulesCfg(daily_loss_limit=300)})
    try:
        await rig.buy(2)
        rig.market.move("NQ", -10)
        await check(rig, risk)
        await rig.sell(2)
        assert risk.is_locked("f1")
        assert rig.f1.account_snapshot().realized_pnl_today == -400.0

        clock.now += timedelta(hours=7, minutes=59)  # 16:59 Chicago: same trading day
        await check(rig, risk)
        assert risk.is_locked("f1")

        clock.now += timedelta(minutes=2)  # 17:01 Chicago: Thursday's session
        await check(rig, risk)
        assert not risk.is_locked("f1") and not store.states["f1"].locked
        assert risk.info()["trading_day"] == "2026-11-12"
        assert rig.f1.account_snapshot().realized_pnl_today == 0.0  # day P&L reset
        assert rig.f1.account_snapshot().balance == 50_000 - 400  # the balance keeps the loss
        assert any("New trading day 2026-11-12" in l.message for l in rig.sink.logs_in(LogCategory.RISK))

        await rig.buy(2)
        assert rig.pos(rig.f1, "NQ") == 2  # copying again
    finally:
        await close(rig, risk)


async def test_breach_while_offline_flattens_after_reconnect():
    import asyncio

    rig, risk, _, _ = await risk_rig({"f1": RiskRulesCfg(daily_loss_limit=300)})
    try:
        await rig.buy(2)
        rig.f1.inject_disconnect()
        await rig.settle()
        rig.market.move("NQ", -10)
        await check(rig, risk)
        assert risk.is_locked("f1") and rig.pos(rig.f1, "NQ") == 2  # could not flatten yet
        rig.f1.restore_connection()
        for _ in range(200):
            await asyncio.sleep(0.02)
            if rig.f1.is_connected:
                break
        await rig.settle()
        await rig.reconcile()
        assert rig.pos(rig.f1, "NQ") == 0
    finally:
        await close(rig, risk)


# ------------------------------------------------------------------ max position


async def test_max_position_caps_the_follower_without_blocking_it():
    rig, risk, _, _ = await risk_rig({"f1": RiskRulesCfg(max_position=3)})
    try:
        await rig.buy(5)
        await check(rig, risk)
        assert rig.positions() == (5, 3, 2, 50)
        assert status(rig, "f1") == "in_sync"  # the capped target is the target
        await rig.buy(2)  # leader 7: f1 stays at 3
        assert rig.pos(rig.f1, "NQ") == 3
        await check(rig, risk)
        events = [e for e in rig.sink.risk_events if e.rule == "max_position"]
        assert len(events) == 1 and "capped at 3" in events[0].message
        assert risk.snapshot("f1")["triggered_today"]["max_position"] == 2

        await rig.sell(5)  # leader 2: below the cap again
        assert rig.pos(rig.f1, "NQ") == 2
        await rig.sell(2)
        assert rig.positions() == (0, 0, 0, 0)
        assert [r for r in rig.sink.orders_for("f1") if r.status is RecordStatus.BLOCKED] == []
    finally:
        await close(rig, risk)


async def test_resting_entries_never_add_up_to_more_than_the_cap():
    rig, risk, _, _ = await risk_rig({"f1": RiskRulesCfg(max_position=3)})
    try:
        await rig.buy(2, type=OrderType.LIMIT, limit_price=NQ_PRICE - 5)
        await rig.buy(2, type=OrderType.LIMIT, limit_price=NQ_PRICE - 10)
        await rig.buy(2, type=OrderType.LIMIT, limit_price=NQ_PRICE - 15)
        # 2 + 1 fit under the cap of 3; the third order is not copied at all
        assert sorted(o.qty for o in rig.working(rig.f1)) == [1, 2]
        rig.market.set_price("NQ", NQ_PRICE - 15)
        await check(rig, risk)
        assert rig.pos(rig.leader, "NQ") == 6 and rig.pos(rig.f1, "NQ") == 3
        assert status(rig, "f1") == "in_sync"
    finally:
        await close(rig, risk)


async def test_market_order_makes_room_by_cancelling_a_resting_entry():
    rig, risk, _, _ = await risk_rig({"f1": RiskRulesCfg(max_position=2)})
    try:
        await rig.buy(2, type=OrderType.LIMIT, limit_price=NQ_PRICE - 10)
        assert [o.qty for o in rig.working(rig.f1)] == [2]  # uses the whole cap
        await rig.buy(2)  # leader buys at market first
        assert rig.pos(rig.f1, "NQ") == 2 and rig.working(rig.f1) == []
        rig.market.set_price("NQ", NQ_PRICE - 10)  # the leader's limit fills too: leader 4
        await check(rig, risk)
        assert rig.pos(rig.leader, "NQ") == 4 and rig.pos(rig.f1, "NQ") == 2
        assert status(rig, "f1") == "in_sync"
    finally:
        await close(rig, risk)


async def test_modified_entry_is_limited_to_the_room():
    rig, risk, _, _ = await risk_rig({"f1": RiskRulesCfg(max_position=3)})
    try:
        await rig.buy(1, type=OrderType.LIMIT, limit_price=NQ_PRICE - 10)
        [leader_order] = rig.working(rig.leader)
        await rig.leader.modify_order(leader_order.id, qty=8)
        await rig.settle()
        assert [o.qty for o in rig.working(rig.f1)] == [3]
        assert [o.qty for o in rig.working(rig.f2)] == [4]  # no cap on f2 (0.5x)
    finally:
        await close(rig, risk)


async def test_lowering_the_cap_trims_an_open_position():
    rig, risk, _, _ = await risk_rig({"f1": RiskRulesCfg(max_position=5)})
    try:
        await rig.buy(4)
        assert rig.pos(rig.f1, "NQ") == 4
        cfg = rig.engine.workers["f1"].cfg
        cfg.risk_max_position = 2
        rig.engine.update_follower(cfg)
        risk.set_rule("f1", RiskRulesCfg(max_position=2))
        await check(rig, risk)
        assert rig.pos(rig.f1, "NQ") == 2
    finally:
        await close(rig, risk)


# ------------------------------------------------------------------ trading hours

HOURS = RiskRulesCfg(trading_hours_enabled=True, trading_start="09:30", trading_end="16:00")


async def test_outside_hours_entries_are_not_copied_and_no_mid_trade_join():
    rig, risk, clock, _ = await risk_rig({"f1": HOURS}, now=WED_08_ET)
    try:
        await rig.buy(2, stop_loss=NQ_PRICE - 20)
        await check(rig, risk)
        assert rig.positions() == (2, 0, 1, 20)
        assert rig.working(rig.f1) == []  # no orphan stop without a position
        assert status(rig, "f1") == "in_sync"  # nothing to do is not "out of sync"
        assert rig.engine.workers["f1"].snapshot()["reduce_only"] is True
        skipped = [r for r in rig.sink.orders_for("f1") if r.action is FollowerAction.SKIPPED]
        assert "Outside allowed trading hours" in skipped[0].error
        assert [e.rule for e in rig.sink.risk_events] == ["trading_hours"]
        assert [r for r in rig.sink.orders_for("f1") if r.action is FollowerAction.RECONCILE] == []

        clock.now = WED_10_ET  # the window opens while the leader is in the trade
        await check(rig, risk)
        assert rig.pos(rig.f1, "NQ") == 0  # does not join mid-trade
        await rig.buy(1)
        assert rig.pos(rig.f1, "NQ") == 0

        await rig.sell(3)  # leader flat
        await check(rig, risk)
        assert rig.engine.workers["f1"].snapshot()["reduce_only"] is False
        await rig.buy(2)
        assert rig.positions() == (2, 2, 1, 20)
    finally:
        await close(rig, risk)


async def test_position_is_kept_after_hours_and_exits_are_still_copied():
    rig, risk, clock, _ = await risk_rig({"f1": HOURS}, now=WED_10_ET)
    try:
        await rig.buy(2, stop_loss=NQ_PRICE - 20, take_profit=NQ_PRICE + 40)
        assert rig.pos(rig.f1, "NQ") == 2

        clock.now = WED_17_ET  # window closed
        await check(rig, risk)
        assert rig.pos(rig.f1, "NQ") == 2 and len(rig.working(rig.f1)) == 2  # kept, still protected

        await rig.buy(2)  # leader adds: not copied
        assert rig.positions() == (4, 2, 2, 40)
        await check(rig, risk)
        assert status(rig, "f1") == "in_sync"

        stop = next(o for o in rig.working(rig.leader) if o.type is OrderType.STOP)
        await rig.leader.modify_order(stop.id, stop_price=NQ_PRICE - 5)  # SL changes are mirrored
        await rig.settle()
        assert [o.stop_price for o in rig.working(rig.f1, OrderType.STOP)] == [NQ_PRICE - 5]

        await rig.sell(3)  # leader down to 1: f1 follows the reduction
        assert rig.positions() == (1, 1, 0, 10)
        await rig.sell(1)
        assert rig.positions() == (0, 0, 0, 0)
        assert rig.working(rig.f1) == []
    finally:
        await close(rig, risk)


async def test_after_hours_limit_entry_is_not_mirrored():
    rig, risk, _, _ = await risk_rig({"f1": HOURS}, now=WED_17_ET)
    try:
        await rig.buy(2, type=OrderType.LIMIT, limit_price=NQ_PRICE - 10)
        assert rig.working(rig.f1) == [] and len(rig.working(rig.f2)) == 1
        rig.market.set_price("NQ", NQ_PRICE - 10)
        await check(rig, risk)
        assert rig.positions() == (2, 0, 1, 20)
    finally:
        await close(rig, risk)


async def test_alert_only_follower_respects_hours_and_reversal_only_closes():
    rig, risk, clock, _ = await risk_rig({"f1": HOURS}, now=WED_10_ET, alert_only=("f1",))
    try:
        await rig.buy(2)
        assert rig.pos(rig.f1, "NQ") == 2
        clock.now = WED_17_ET
        await check(rig, risk)
        await rig.leader.place_order(OrderRequest("NQ", Side.SELL, 5))  # leader reverses to -3
        await rig.settle()
        assert rig.pos(rig.leader, "NQ") == -3
        assert rig.pos(rig.f1, "NQ") == 0  # closed, not reversed
        assert rig.pos(rig.f2, "NQ") == -1  # f2 has no hours rule
    finally:
        await close(rig, risk)
