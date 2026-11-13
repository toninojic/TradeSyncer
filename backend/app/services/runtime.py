"""Owns everything that lives for the whole app: the simulated market, the
broker adapters, the copy engine, the DB writer and the WebSocket hub."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.brokers.base import AccountSnapshot, BrokerAdapter, BrokerError, Order, Position
from app.brokers.sim import SimMarket, SimulatedBroker
from app.brokers.tradovate import TradovateAdapter
from app.config import Settings
from app.core.security import CredentialCipher
from app.db.models import Account
from app.db.repo import follower_config, get_setting, list_accounts, risk_rules_cfg, set_setting
from app.engine.config import EngineSettings
from app.engine.copy_engine import CopyEngine
from app.engine.follower_worker import ReconcileTick
from app.engine.events import FanoutSink, LogCategory, LogLevel, LogRecord
from app.engine.risk import RiskManager
from app.services.risk_store import DbRiskStore
from app.services.sinks import DbSink, HubSink
from app.services.ws_hub import WsHub

log = logging.getLogger(__name__)

TRADOVATE_FIELDS = {"username", "password", "app_id", "cid", "secret", "environment"}


@dataclass(slots=True)
class AccountInfo:
    id: int
    name: str
    role: str
    broker_type: str
    enabled: bool


@dataclass(slots=True)
class AccountCache:
    snapshot: AccountSnapshot | None = None
    positions: list[Position] | None = None
    orders: list[Order] | None = None
    updated: float = 0.0


class Runtime:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        cipher: CredentialCipher,
        engine_settings: EngineSettings | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.cipher = cipher
        self.engine_settings = engine_settings or EngineSettings(
            reconcile_interval_s=settings.reconcile_interval_s
        )
        self.market = SimMarket(tick_interval=settings.sim_tick_interval_s)
        self.adapters: dict[str, BrokerAdapter] = {}
        self._fingerprints: dict[str, tuple] = {}
        self.accounts: dict[str, AccountInfo] = {}
        self.cache: dict[str, AccountCache] = {}
        self.engine: CopyEngine | None = None
        self.status_note: str | None = None
        self.hub = WsHub()
        self.db_sink = DbSink(sessionmaker)
        self.sink = FanoutSink(self.db_sink, HubSink(self))
        self.risk = RiskManager(sink=self.sink, store=DbRiskStore(sessionmaker))
        self.sessions: dict[str, float] = {}  # session id -> expiry (epoch)
        self.login_failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._dirty = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ================================================================== lifecycle

    async def start(self) -> None:
        self.db_sink.start()
        self.market.add_listener(self._on_price)
        if self.settings.sim_random_walk:
            self.market.start()
        await self.risk.start()
        await self.rebuild("startup")
        self._tasks = [
            asyncio.create_task(self._broadcast_loop(), name="ws-broadcast"),
            asyncio.create_task(self._poll_accounts_loop(), name="account-poller"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        await self.risk.stop()
        if self.engine:
            await self.engine.stop(disconnect=True)
            self.engine = None
        await self.market.stop()
        await self.db_sink.stop()

    # ================================================================== engine (re)build

    async def rebuild(self, reason: str) -> None:
        """(Re)create the copy engine from the accounts in the database.

        Adapters are reused when an account's broker settings did not change, so
        simulated positions survive a rebuild."""
        async with self._lock:
            async with self.sessionmaker() as session:
                accounts = await list_accounts(session)
                running = await get_setting(session, "copier_running", True)

            self.risk.attach(None, self._day_pnl, self._reset_day)
            if self.engine is not None:
                await self.engine.stop(disconnect=True)
                self.engine = None

            wanted = {a.engine_id: a for a in accounts}
            for account_id in list(self.adapters):
                account = wanted.get(account_id)
                if account is None or self._fingerprint(account) != self._fingerprints.get(account_id):
                    self._dispose_adapter(account_id)
            for account in accounts:
                if account.engine_id not in self.adapters:
                    try:
                        self.adapters[account.engine_id] = self._create_adapter(account)
                        self._fingerprints[account.engine_id] = self._fingerprint(account)
                    except Exception as exc:
                        await self.log(LogLevel.ERROR, LogCategory.CONNECTION,
                                       f"Cannot create adapter: {exc}", account.engine_id)
            self.accounts = {
                a.engine_id: AccountInfo(a.id, a.name, a.role, a.broker_type, a.enabled) for a in accounts
            }
            self.cache = {aid: self.cache.get(aid, AccountCache()) for aid in self.adapters}

            enabled = [a for a in accounts if a.enabled and a.engine_id in self.adapters]
            leaders = [a for a in enabled if a.role == "leader"]
            self.risk.set_rules(
                {a.engine_id: risk_rules_cfg(a) for a in enabled if a.role == "follower"}
            )
            if len(leaders) != 1:
                self.status_note = (
                    "No enabled leader account" if not leaders
                    else "More than one enabled leader account"
                )
                await self.log(LogLevel.WARNING, LogCategory.ENGINE,
                               f"Copier not started: {self.status_note}")
                self.mark_dirty()
                return
            self.status_note = None
            followers = [
                (self.adapters[a.engine_id], follower_config(a))
                for a in enabled if a.role == "follower"
            ]
            self.engine = CopyEngine(
                self.adapters[leaders[0].engine_id], followers,
                sink=self.sink, settings=self.engine_settings, risk=self.risk,
            )
            self.risk.attach(self.engine, self._day_pnl, self._reset_day)
            await self.engine.start(running=bool(running))
            await self.log(LogLevel.INFO, LogCategory.ENGINE,
                           f"Copier (re)built ({reason}): leader {leaders[0].name}, "
                           f"{len(followers)} follower(s)")
            self.mark_dirty()

    @staticmethod
    def _fingerprint(account: Account) -> tuple:
        return (account.broker_type, account.credentials_enc, account.sim_starting_balance)

    def _create_adapter(self, account: Account) -> BrokerAdapter:
        if account.broker_type == "sim":
            return SimulatedBroker(account.engine_id, self.market,
                                   starting_balance=account.sim_starting_balance)
        if account.broker_type == "tradovate":
            creds = self.cipher.decrypt(account.credentials_enc) if account.credentials_enc else {}
            return TradovateAdapter(account.engine_id,
                                    **{k: v for k, v in creds.items() if k in TRADOVATE_FIELDS})
        raise ValueError(f"Unknown broker type {account.broker_type!r}")

    def _dispose_adapter(self, account_id: str) -> None:
        adapter = self.adapters.pop(account_id, None)
        self._fingerprints.pop(account_id, None)
        self.cache.pop(account_id, None)
        if isinstance(adapter, SimulatedBroker):
            adapter.close()

    async def apply_follower_settings(self, account_id: int) -> None:
        """Push changed copy settings and risk rules to a running follower
        without a rebuild."""
        async with self.sessionmaker() as session:
            account = await session.get(Account, account_id)
            if account is None:
                return
            cfg = follower_config(account)
            rules = risk_rules_cfg(account)
            info = self.accounts.get(account.engine_id)
            if info:
                info.name = account.name
        if self.engine and cfg.account_id in self.engine.workers:
            self.risk.set_rule(cfg.account_id, rules)
            self.engine.update_follower(cfg)
            self.engine.workers[cfg.account_id].submit(ReconcileTick())
        self.mark_dirty()

    # ================================================================== risk

    def _day_pnl(self, follower_id: str) -> float | None:
        adapter = self.adapters.get(follower_id)
        if isinstance(adapter, SimulatedBroker):
            return adapter.account_snapshot().day_pnl
        cache = self.cache.get(follower_id)
        return cache.snapshot.day_pnl if cache and cache.snapshot else None

    def _reset_day(self) -> None:
        """New trading day: simulated accounts start with a day P&L of zero."""
        for adapter in self.adapters.values():
            if isinstance(adapter, SimulatedBroker):
                adapter.reset_day()
        self.mark_dirty()

    async def unlock_risk(self, account_id: int) -> bool:
        unlocked = await self.risk.unlock(str(account_id))
        self.mark_dirty()
        return unlocked

    async def new_trading_day(self) -> None:
        await self.risk.start_new_day()
        self.mark_dirty()

    # ================================================================== controls

    async def set_running(self, running: bool) -> None:
        async with self.sessionmaker() as session:
            await set_setting(session, "copier_running", running)
            await session.commit()
        if self.engine:
            if running:
                await self.engine.resume()
            else:
                await self.engine.pause()
        self.mark_dirty()

    async def kill_switch(self) -> None:
        async with self.sessionmaker() as session:
            await set_setting(session, "copier_running", False)
            await session.commit()
        if self.engine:
            await self.engine.kill_switch("Kill switch activated from the admin panel")
        self.mark_dirty()

    async def flatten_account(self, account_id: int) -> None:
        key = str(account_id)
        if self.engine and key in self.engine.workers:
            await self.engine.flatten_follower(key, "Manual flatten from the admin panel")
        elif key in self.adapters:
            adapter = self.adapters[key]
            await adapter.flatten()
            await self.log(LogLevel.WARNING, LogCategory.KILL_SWITCH,
                           "Manual flatten from the admin panel", key)
        else:
            raise KeyError(account_id)
        self.mark_dirty()

    async def retry_sync(self, account_id: int) -> None:
        key = str(account_id)
        if not self.engine or key not in self.engine.workers:
            raise KeyError(account_id)
        await self.engine.retry_sync(key)

    # ================================================================== simulator helpers

    def sim_adapter(self, account_id: int) -> SimulatedBroker:
        adapter = self.adapters.get(str(account_id))
        if not isinstance(adapter, SimulatedBroker):
            raise LookupError("Not a simulated account")
        return adapter

    def leader_info(self) -> AccountInfo | None:
        for info in self.accounts.values():
            if info.role == "leader" and info.enabled:
                return info
        return None

    # ================================================================== status snapshot

    def copier_state(self) -> str:
        if self.engine is None:
            return "no_leader"
        if self.engine.killed:
            return "killed"
        return "running" if self.engine.running else "paused"

    def snapshot(self) -> dict[str, Any]:
        engine = self.engine.snapshot() if self.engine else None
        workers = {f["account_id"]: f for f in engine["followers"]} if engine else {}
        leader_id = engine["leader"]["account_id"] if engine else None
        accounts = []
        for key, info in self.accounts.items():
            adapter = self.adapters.get(key)
            acct, positions, orders = self._account_data(key, adapter)
            entry: dict[str, Any] = {
                "id": info.id, "name": info.name, "role": info.role,
                "broker_type": info.broker_type, "enabled": info.enabled,
                "is_active_leader": key == leader_id,
                "connection": str(adapter.status) if adapter else "error",
                "balance": acct.balance if acct else None,
                "realized_pnl": acct.realized_pnl_today if acct else None,
                "unrealized_pnl": acct.unrealized_pnl if acct else None,
                "day_pnl": acct.day_pnl if acct else None,
                "positions": [
                    {"symbol": p.symbol, "qty": p.qty, "avg_price": p.avg_price,
                     "unrealized_pnl": p.unrealized_pnl} for p in positions
                ],
                "working_orders": len(orders),
            }
            worker = workers.get(key)
            if worker is not None:
                entry["copy"] = {k: v for k, v in worker.items() if k not in ("account_id", "name")}
            risk = self.risk.snapshot(key)
            if risk is not None:
                entry["risk"] = risk
            accounts.append(entry)
        return {
            **self.risk.info(),
            "copier": {
                "state": self.copier_state(),
                "note": self.status_note,
                "leader_id": int(leader_id) if leader_id else None,
            },
            "accounts": accounts,
            "prices": self.market.prices(),
            "random_walk": self.market.walk_running,
            "server_time": time.time(),
        }

    def _account_data(
        self, key: str, adapter: BrokerAdapter | None
    ) -> tuple[AccountSnapshot | None, list[Position], list[Order]]:
        if isinstance(adapter, SimulatedBroker):
            return adapter.account_snapshot(), adapter.positions_snapshot(), adapter.orders_snapshot()
        cache = self.cache.get(key)
        if cache is None:
            return None, [], []
        return cache.snapshot, cache.positions or [], cache.orders or []

    def account_orders(self, account_id: int) -> list[Order]:
        key = str(account_id)
        _, _, orders = self._account_data(key, self.adapters.get(key))
        return orders

    # ================================================================== background loops

    def mark_dirty(self) -> None:
        self._dirty.set()

    def _on_price(self, underlying: str, price: float) -> None:
        self._dirty.set()
        self.risk.poke()  # unrealized P&L changed: re-check the daily loss limits

    async def _broadcast_loop(self) -> None:
        last = 0.0
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            # at most 4 snapshots per second
            wait = 0.25 - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._dirty.clear()
            last = time.monotonic()
            if self.hub.has_clients:
                try:
                    self.hub.publish({"type": "snapshot", "data": self.snapshot()})
                except Exception:
                    log.exception("Snapshot failed")

    async def _poll_accounts_loop(self) -> None:
        """Real broker adapters are polled for balances/positions; the simulator
        is read directly."""
        while True:
            await asyncio.sleep(1.0)
            for key, adapter in list(self.adapters.items()):
                if isinstance(adapter, SimulatedBroker) or not adapter.is_connected:
                    continue
                try:
                    snapshot = await asyncio.wait_for(adapter.get_account(), 2)
                    positions = await asyncio.wait_for(adapter.get_positions(), 2)
                    orders = await asyncio.wait_for(adapter.get_orders(), 2)
                except (BrokerError, asyncio.TimeoutError, NotImplementedError):
                    continue
                self.cache[key] = AccountCache(snapshot, positions, orders, time.time())

    # ================================================================== logging

    async def log(self, level: LogLevel, category: LogCategory, message: str,
                  account_id: str | None = None, data: dict | None = None) -> None:
        log.log({"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}[level],
                "[%s] %s", category, message)
        await self.sink.emit(LogRecord(level, category, message, account_id, data))
