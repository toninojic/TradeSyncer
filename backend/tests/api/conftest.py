from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.security import CredentialCipher, hash_password
from app.engine.config import EngineSettings
from app.main import create_app

PASSWORD = "correct-horse-42"

# Seed account ids
LEADER, F1, F2, F3 = 1, 2, 3, 4


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        admin_username="admin",
        admin_password_hash=hash_password(PASSWORD),
        session_secret="test-session-secret-" + "x" * 32,
        encryption_key=CredentialCipher.generate_key(),
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        sim_random_walk=False,
        seed_demo_data=True,
    )
    values.update(overrides)
    return Settings(**values)


FAST_ENGINE = EngineSettings(reconcile_interval_s=0.2, linked_fill_grace_s=0.3, stale_market_order_s=0.5)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def anon(settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings, FAST_ENGINE)) as client:
        yield client


@pytest.fixture
def client(anon) -> TestClient:
    r = anon.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    wait_for(anon, lambda s: s["copier"]["state"] == "running"
             and all(a["connection"] == "connected" for a in s["accounts"]))
    return anon


def wait_for(client: TestClient, predicate: Callable[[dict], bool], timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    status = {}
    while time.time() < deadline:
        status = client.get("/api/status").json()
        if predicate(status):
            return status
        time.sleep(0.05)
    raise AssertionError(f"condition not met; last status: {status}")


def positions(status: dict) -> dict[int, dict[str, int]]:
    return {a["id"]: {p["symbol"]: p["qty"] for p in a["positions"]} for a in status["accounts"]}


def account(status: dict, account_id: int) -> dict:
    return next(a for a in status["accounts"] if a["id"] == account_id)


def wait_trades(client: TestClient, predicate: Callable[[list], bool], timeout: float = 5.0, **params) -> list:
    deadline = time.time() + timeout
    items: list = []
    while time.time() < deadline:
        items = client.get("/api/trades", params=params).json()["items"]
        if predicate(items):
            return items
        time.sleep(0.05)
    raise AssertionError(f"trade log condition not met: {items}")
