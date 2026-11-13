"""Normal entry/exit and scale in/out."""

import pytest

from app.engine.events import FollowerAction, RecordStatus

pytestmark = pytest.mark.integration


async def test_market_entry_and_exit(rig):
    await rig.buy(2)
    assert rig.positions() == (2, 2, 1, 20)

    rig.market.move("NQ", 10)  # +10 points
    leader_pnl = rig.leader.account_snapshot().unrealized_pnl
    f3_pnl = rig.f3.account_snapshot().unrealized_pnl
    assert leader_pnl == 400.0  # 2 NQ x 10 pt x $20
    assert f3_pnl == 400.0  # 20 MNQ x 10 pt x $2

    await rig.sell(2)
    assert rig.positions() == (0, 0, 0, 0)
    assert rig.leader.account_snapshot().realized_pnl_today == 400.0
    assert rig.f1.account_snapshot().realized_pnl_today == 400.0
    assert rig.f2.account_snapshot().realized_pnl_today == 200.0
    assert rig.f3.account_snapshot().realized_pnl_today == 400.0


async def test_short_and_reverse(rig):
    await rig.sell(2)
    assert rig.positions() == (-2, -2, -1, -20)
    await rig.buy(4)  # reverse to long 2
    assert rig.positions() == (2, 2, 1, 20)
    await rig.sell(2)
    assert rig.positions() == (0, 0, 0, 0)


async def test_scale_in_and_out_uses_position_target(rig):
    await rig.buy(1)
    assert rig.positions() == (1, 1, 0, 10)  # 0.5 x 1 rounds down to 0
    await rig.buy(1)
    assert rig.positions() == (2, 2, 1, 20)  # 0.5 x 2 = 1
    await rig.buy(1)
    assert rig.positions() == (3, 3, 1, 30)
    await rig.sell(1)
    assert rig.positions() == (2, 2, 1, 20)
    await rig.sell(1)
    assert rig.positions() == (1, 1, 0, 10)
    await rig.sell(1)
    assert rig.positions() == (0, 0, 0, 0)


async def test_latency_is_measured_and_skips_are_logged(rig):
    await rig.buy(1)
    copies = [r for r in rig.sink.follower_orders.values() if r.action is FollowerAction.COPY_FILL]
    assert {r.follower_id for r in copies} == {"f1", "f3"}
    assert all(r.latency_ms is not None and r.latency_ms >= 0 for r in copies)
    assert all(r.status is RecordStatus.FILLED for r in copies)
    skipped = [r for r in rig.sink.orders_for("f2") if r.action is FollowerAction.SKIPPED]
    assert len(skipped) == 1 and "rounds to 0" in skipped[0].error


async def test_reconcile_reports_in_sync(rig):
    await rig.buy(2)
    await rig.reconcile()
    statuses = {f["account_id"]: f["sync_status"] for f in rig.engine.snapshot()["followers"]}
    assert statuses == {"f1": "in_sync", "f2": "in_sync", "f3": "in_sync"}
