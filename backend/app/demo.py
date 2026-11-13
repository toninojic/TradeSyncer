"""Terminal demo of the copy engine (milestone 1, before the API and UI exist).

Run from the backend folder:  python -m app.demo

It starts a simulated market, 1 leader and the 3 seed followers, then plays a
scripted session and prints positions and the trade log after each step.
"""

from __future__ import annotations

import asyncio
import logging

from app.brokers.base import OrderRequest, OrderType, Side
from app.brokers.sim import SimMarket, SimulatedBroker
from app.engine.config import ContractMapping, EngineSettings, FollowerConfig
from app.engine.copy_engine import CopyEngine
from app.engine.events import MemorySink

START_PRICE = 25000.0


class Demo:
    def __init__(self) -> None:
        self.market = SimMarket({"NASDAQ100": START_PRICE})
        self.leader = SimulatedBroker("leader", self.market)
        self.f1 = SimulatedBroker("f1-1x-NQ", self.market)
        self.f2 = SimulatedBroker("f2-0.5x-NQ", self.market)
        self.f3 = SimulatedBroker("f3-NQ-to-MNQ", self.market)
        self.sink = MemorySink()
        configs = [
            FollowerConfig(self.f1.account_id, name="1x NQ"),
            FollowerConfig(self.f2.account_id, name="0.5x NQ", multiplier=0.5),
            FollowerConfig(self.f3.account_id, name="NQ->MNQ 10:1",
                           mappings=[ContractMapping("NQ", "MNQ", 10)]),
        ]
        self.engine = CopyEngine(
            self.leader, zip([self.f1, self.f2, self.f3], configs), sink=self.sink,
            settings=EngineSettings(reconcile_interval_s=3600, linked_fill_grace_s=0.3),
        )
        self._printed_orders: set[str] = set()

    async def settle(self) -> None:
        await self.engine.wait_idle()

    async def step(self, title: str) -> None:
        await self.settle()
        print(f"\n=== {title}  (NQ price {self.market.price('NQ'):.2f})")
        for broker in (self.leader, self.f1, self.f2, self.f3):
            pos = ", ".join(f"{p.symbol} {p.qty:+d}" for p in broker.positions_snapshot()) or "flat"
            work = ", ".join(
                f"{o.side} {o.qty} {o.symbol} {o.type}@{o.price}" for o in broker.orders_snapshot()
            ) or "-"
            acct = broker.account_snapshot()
            print(f"  {broker.account_id:<14} pos: {pos:<12} day P&L: {acct.day_pnl:>9.2f}   working: {work}")
        new = [r for r in self.sink.follower_orders.values() if r.id not in self._printed_orders]
        for r in sorted(new, key=lambda r: r.created_at):
            self._printed_orders.add(r.id)
            latency = f"{r.latency_ms:.1f} ms" if r.latency_ms is not None else ""
            detail = f"{r.side or ''} {r.qty or ''} {r.symbol or ''}".strip()
            print(f"    -> {r.follower_id:<14} {r.action:<12} {detail:<14} {r.status:<10} {latency:<9} {r.error or ''}")

    async def run(self) -> None:
        await self.engine.start(reconcile_loop=False)
        while not (self.engine.leader_initialized
                   and all(w.initialized for w in self.engine.workers.values())):
            await asyncio.sleep(0.01)
        await self.step("Connected")

        await self.leader.place_order(OrderRequest("NQ", Side.BUY, 2, stop_loss=24980, take_profit=25040))
        await self.step("Leader buys 2 NQ at market with SL 24980 / TP 25040")

        self.market.move("NQ", 12)
        await self.step("Price +12 points")

        stop = next(o for o in self.leader.orders_snapshot() if o.type is OrderType.STOP)
        await self.leader.modify_order(stop.id, stop_price=25000)
        await self.step("Leader moves SL to break-even (25000)")

        await self.leader.place_order(OrderRequest("NQ", Side.SELL, 1))
        await self.step("Leader partially closes 1 NQ (brackets shrink; f2 at 0.5x is now flat)")

        self.market.set_price("NQ", 25040)
        await self.step("Price reaches the TP: everyone exits on their own TP order")

        self.f1.reject_next_orders(1, "Simulated: insufficient margin")
        self.f2.inject_disconnect(duration_s=0.5)
        await self.leader.place_order(OrderRequest("NQ", Side.SELL, 2))
        await self.step("Leader shorts 2 NQ; f1 gets a rejection, f2 is offline")

        while not self.f2.is_connected:  # reconnects by itself with backoff
            await asyncio.sleep(0.05)
        await self.engine.reconcile_now()
        await self.step("f2 reconnected; reconciliation fixed f1 and f2")

        self.leader.duplicate_next_events(5)
        await self.leader.place_order(OrderRequest("NQ", Side.BUY, 1))
        await self.step("Leader covers 1 (its events arrive twice; duplicates are ignored)")

        await self.engine.kill_switch()
        await self.step("KILL SWITCH: followers flattened, copying stopped")

        acc = {w.id: w.snapshot()["avg_latency_ms"] for w in self.engine.workers.values()}
        print(f"\nAverage copy latency (fill received -> follower ack): {acc}")
        print(f"System log entries: {len(self.sink.logs)}; trade log records: {len(self.sink.follower_orders)}")
        await self.engine.stop()


def main() -> None:
    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(Demo().run())


if __name__ == "__main__":
    main()
