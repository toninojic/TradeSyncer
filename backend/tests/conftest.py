"""Shared fixtures: a simulated market with 1 leader and the 3 seed followers."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.brokers.base import OrderRequest, OrderType, Side
from app.brokers.sim import SimMarket, SimulatedBroker
from app.engine.config import ContractMapping, EngineSettings, FollowerConfig, MismatchMode
from app.engine.copy_engine import CopyEngine
from app.engine.events import MemorySink

NQ_PRICE = 25000.0


@dataclass
class Rig:
    market: SimMarket
    leader: SimulatedBroker
    f1: SimulatedBroker  # 1x NQ
    f2: SimulatedBroker  # 0.5x NQ
    f3: SimulatedBroker  # NQ -> MNQ 10:1
    engine: CopyEngine
    sink: MemorySink

    @property
    def followers(self) -> list[SimulatedBroker]:
        return [self.f1, self.f2, self.f3]

    async def settle(self, timeout: float = 5.0) -> None:
        await self.engine.wait_idle(timeout)

    async def reconcile(self) -> None:
        await self.engine.reconcile_now()
        await self.settle()

    async def buy(self, qty: int, symbol: str = "NQ", **kw) -> None:
        await self.leader.place_order(OrderRequest(symbol, Side.BUY, qty, **kw))
        await self.settle()

    async def sell(self, qty: int, symbol: str = "NQ", **kw) -> None:
        await self.leader.place_order(OrderRequest(symbol, Side.SELL, qty, **kw))
        await self.settle()

    def pos(self, broker: SimulatedBroker, symbol: str) -> int:
        return next((p.qty for p in broker.positions_snapshot() if p.symbol == symbol), 0)

    def positions(self) -> tuple[int, int, int, int]:
        return (
            self.pos(self.leader, "NQ"),
            self.pos(self.f1, "NQ"),
            self.pos(self.f2, "NQ"),
            self.pos(self.f3, "MNQ"),
        )

    def working(self, broker: SimulatedBroker, type_: OrderType | None = None):
        return [o for o in broker.orders_snapshot() if type_ is None or o.type is type_]


def follower_configs() -> list[FollowerConfig]:
    return [
        FollowerConfig("f1", name="1x NQ", multiplier=1.0),
        FollowerConfig("f2", name="0.5x NQ", multiplier=0.5),
        FollowerConfig(
            "f3", name="NQ->MNQ 10:1", multiplier=1.0,
            mappings=[ContractMapping("NQ", "MNQ", 10)],
        ),
    ]


async def make_rig(
    settings: EngineSettings | None = None,
    configs: list[FollowerConfig] | None = None,
    reconcile_loop: bool = False,
    risk=None,
) -> Rig:
    market = SimMarket({"NASDAQ100": NQ_PRICE}, seed=1)
    leader = SimulatedBroker("leader", market)
    followers = [SimulatedBroker(cid, market) for cid in ("f1", "f2", "f3")]
    sink = MemorySink()
    settings = settings or EngineSettings(
        reconcile_interval_s=0.05 if reconcile_loop else 3600,
        linked_fill_grace_s=0.3,
        stale_market_order_s=0.5,
    )
    configs = configs or follower_configs()
    engine = CopyEngine(leader, zip(followers, configs), sink=sink, settings=settings, risk=risk)
    if risk is not None:
        brokers = {b.account_id: b for b in followers}
        risk.sink = sink
        risk.attach(
            engine,
            lambda fid: brokers[fid].account_snapshot().day_pnl,
            lambda: [b.reset_day() for b in [leader, *followers]],
        )
    await engine.start(reconcile_loop=reconcile_loop)
    rig = Rig(market, leader, *followers, engine=engine, sink=sink)
    await _wait_connected(rig)
    await rig.settle()
    return rig


async def _wait_connected(rig: Rig) -> None:
    import asyncio

    for _ in range(200):
        if rig.engine.leader_initialized and all(
            w.initialized for w in rig.engine.workers.values()
        ):
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("rig did not connect")


@pytest.fixture
async def rig():
    r = await make_rig()
    yield r
    await r.engine.stop()


def alert_only(configs: list[FollowerConfig], *ids: str) -> list[FollowerConfig]:
    for cfg in configs:
        if cfg.account_id in ids:
            cfg.mismatch_mode = MismatchMode.ALERT_ONLY
    return configs
