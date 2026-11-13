from datetime import date, datetime, timezone

import pytest

from app.brokers.base import OrderRequest, Side
from app.core.trading_day import next_roll, trading_day, within_trading_hours
from app.engine.config import FollowerConfig
from app.engine.risk import (
    AllowAll,
    MemoryRiskStore,
    RiskManager,
    RiskRulesCfg,
    RiskState,
    loss_used,
)
from app.engine.sizing import follower_targets, reduce_only_target, reduces_exposure


def utc(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ------------------------------------------------------------------ trading day (CME session)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (utc(2026, 11, 11, 22, 59), date(2026, 11, 11)),  # Wed 16:59 Chicago (CST)
        (utc(2026, 11, 11, 23, 0), date(2026, 11, 12)),  # Wed 17:00 Chicago -> Thursday
        (utc(2026, 11, 13, 22, 59), date(2026, 11, 13)),  # Fri before the close
        (utc(2026, 11, 13, 23, 0), date(2026, 11, 16)),  # Fri 17:00 -> Monday
        (utc(2026, 11, 14, 12, 0), date(2026, 11, 16)),  # Saturday -> Monday
        (utc(2026, 11, 15, 23, 30), date(2026, 11, 16)),  # Sunday evening session -> Monday
        (utc(2026, 7, 1, 21, 59), date(2026, 7, 1)),  # summer: 16:59 Chicago (CDT)
        (utc(2026, 7, 1, 22, 0), date(2026, 7, 2)),  # summer: 17:00 Chicago
    ],
)
def test_trading_day(now, expected):
    assert trading_day(now) == expected


def test_next_roll():
    assert next_roll(utc(2026, 11, 11, 12, 0)) == utc(2026, 11, 11, 23, 0)
    assert next_roll(utc(2026, 11, 11, 23, 0)) == utc(2026, 11, 12, 23, 0)
    assert next_roll(utc(2026, 11, 13, 23, 30)) == utc(2026, 11, 16, 23, 0)  # Fri evening -> Mon 17:00
    assert next_roll(utc(2026, 11, 14, 12, 0)) == utc(2026, 11, 16, 23, 0)  # weekend
    assert next_roll(utc(2026, 7, 1, 12, 0)) == utc(2026, 7, 1, 22, 0)  # daylight saving time


# ------------------------------------------------------------------ trading hours (New York time)

WEEKDAYS = (0, 1, 2, 3, 4)


@pytest.mark.parametrize(
    ("now", "inside"),
    [
        (utc(2026, 11, 11, 14, 29), False),  # Wed 09:29 ET
        (utc(2026, 11, 11, 14, 30), True),  # 09:30 ET
        (utc(2026, 11, 11, 20, 59), True),  # 15:59 ET
        (utc(2026, 11, 11, 21, 0), False),  # 16:00 ET: end is exclusive
        (utc(2026, 11, 14, 15, 0), False),  # Saturday
        (utc(2026, 7, 1, 13, 30), True),  # summer: 09:30 EDT
    ],
)
def test_within_trading_hours(now, inside):
    assert within_trading_hours(now, "09:30", "16:00", WEEKDAYS) is inside


def test_trading_hours_across_midnight_belong_to_the_start_day():
    # 18:00-02:00 on Fridays only
    assert within_trading_hours(utc(2026, 11, 13, 23, 30), "18:00", "02:00", (4,))  # Fri 18:30 ET
    assert within_trading_hours(utc(2026, 11, 14, 6, 0), "18:00", "02:00", (4,))  # Sat 01:00 ET
    assert not within_trading_hours(utc(2026, 11, 14, 8, 0), "18:00", "02:00", (4,))  # Sat 03:00 ET
    assert not within_trading_hours(utc(2026, 11, 15, 6, 0), "18:00", "02:00", (4,))  # Sun 01:00 ET


def test_trading_hours_same_start_and_end_is_the_whole_day():
    assert within_trading_hours(utc(2026, 11, 11, 3, 0), "00:00", "00:00", WEEKDAYS)  # Tue 22:00 ET
    assert not within_trading_hours(utc(2026, 11, 14, 15, 0), "00:00", "00:00", WEEKDAYS)


# ------------------------------------------------------------------ pure helpers


def test_loss_used_is_measured_from_the_baseline():
    assert loss_used(-250.0, 0.0) == 250.0
    assert loss_used(120.0, 0.0) == 0.0  # profit uses nothing
    assert loss_used(-450.0, -400.0) == 50.0  # after a manual re-enable at -400


@pytest.mark.parametrize(
    ("target", "position", "expected"),
    [
        (5, 2, 2),  # leader adds: stay
        (1, 2, 1),  # leader reduces: follow
        (0, 2, 0),  # leader closes: close
        (-3, 2, 0),  # leader reverses: close only
        (-5, -2, -2),
        (-1, -2, -1),
        (4, -2, 0),
        (3, 0, 0),  # flat: no entry
    ],
)
def test_reduce_only_target(target, position, expected):
    assert reduce_only_target(target, position) == expected


def test_reduces_exposure():
    assert reduces_exposure(2, -1)
    assert reduces_exposure(2, -2)
    assert reduces_exposure(-3, 1)
    assert not reduces_exposure(2, 1)  # adds
    assert not reduces_exposure(0, 1)  # opens
    assert not reduces_exposure(2, -3)  # reverses
    assert not reduces_exposure(5, -8)  # reverses even though |after| < |before|


def test_position_cap_is_the_tighter_of_both_caps():
    assert FollowerConfig("f").position_cap is None
    assert FollowerConfig("f", max_contracts=10).position_cap == 10
    assert FollowerConfig("f", risk_max_position=4).position_cap == 4
    cfg = FollowerConfig("f", max_contracts=10, risk_max_position=4)
    assert cfg.position_cap == 4
    assert follower_targets({"NQ": 9}, cfg)["NQ"] == 4
    assert follower_targets({"NQ": -9}, cfg)["NQ"] == -4
    assert follower_targets({"NQ": 9}, cfg, capped=False)["NQ"] == 9


# ------------------------------------------------------------------ RiskManager guard


def manager(now: datetime, **rules) -> RiskManager:
    risk = RiskManager(now_fn=lambda: now)
    risk.set_rules({"f1": RiskRulesCfg(**rules)})
    return risk


def buy(qty: int) -> OrderRequest:
    return OrderRequest("NQ", Side.BUY, qty)


def test_guard_allows_everything_without_rules():
    assert AllowAll().check_order("f1", buy(100), 0, False) is None
    assert manager(utc(2026, 11, 11, 15)).check_order("f1", buy(100), 0, False) is None
    assert manager(utc(2026, 11, 11, 15)).check_order("unknown", buy(100), 0, False) is None


def test_guard_max_position():
    risk = manager(utc(2026, 11, 11, 15), max_position=3)
    assert risk.check_order("f1", buy(3), 0, False) is None
    assert "Max position 3" in risk.check_order("f1", buy(4), 0, False)
    assert "Max position 3" in risk.check_order("f1", buy(1), 3, False)
    assert risk.check_order("f1", OrderRequest("NQ", Side.SELL, 10), 3, True) is None  # exits pass


def test_guard_trading_hours():
    outside = manager(utc(2026, 11, 11, 13), trading_hours_enabled=True)  # 08:00 ET
    assert "Outside allowed trading hours" in outside.check_order("f1", buy(1), 0, False)
    assert outside.check_order("f1", OrderRequest("NQ", Side.SELL, 1), 2, True) is None
    inside = manager(utc(2026, 11, 11, 15), trading_hours_enabled=True)  # 10:00 ET
    assert inside.check_order("f1", buy(1), 0, False) is None
    assert not inside.outside_hours("f1") and outside.outside_hours("f1")


async def test_notes_are_written_once_per_day_and_counted():
    risk = manager(utc(2026, 11, 11, 15), max_position=1)
    for _ in range(3):
        risk.check_order("f1", buy(2), 0, False)
    await risk.evaluate()
    assert len(risk.sink.risk_events) == 1
    assert risk.sink.risk_events[0].rule == "max_position"
    assert risk.sink.risk_events[0].trading_day == "2026-11-11"
    assert risk.snapshot("f1")["triggered_today"] == {"max_position": 3}


async def test_lock_from_today_is_restored_and_an_old_one_expires():
    now = utc(2026, 11, 11, 15)
    store = MemoryRiskStore()
    store.states["f1"] = RiskState(locked=True, locked_day="2026-11-11", locked_at=1.0, reason="x")
    store.states["f2"] = RiskState(locked=True, locked_day="2026-11-10", locked_at=1.0, reason="x")
    store.states["f3"] = RiskState(baseline=-500.0, baseline_day="2026-11-10")
    risk = RiskManager(store=store, now_fn=lambda: now)
    await risk.start()
    try:
        assert risk.is_locked("f1")
        assert not risk.is_locked("f2")
        assert store.states["f2"].locked is False  # the expiry was saved
        assert risk._state["f3"].baseline == 0.0
    finally:
        await risk.stop()
