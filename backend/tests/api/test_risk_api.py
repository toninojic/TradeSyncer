"""Risk rules through the API: live rule changes, breach, re-enable, new trading day."""

from fastapi.testclient import TestClient

from app.main import create_app

from tests.api.conftest import (
    F1,
    F2,
    F3,
    FAST_ENGINE,
    LEADER,
    PASSWORD,
    account,
    positions,
    wait_for,
    wait_json,
    wait_risk_events,
    wait_trades,
)


def set_rules(client: TestClient, account_id: int, **changes) -> dict:
    rules = client.get(f"/api/accounts/{account_id}/risk-rules").json()
    rules.update(changes)
    r = client.put(f"/api/accounts/{account_id}/risk-rules", json=rules)
    assert r.status_code == 200, r.text
    return r.json()


def breach_f1(client: TestClient) -> None:
    set_rules(client, F1, daily_loss_limit=300)
    client.post("/api/sim/order", json={"side": "buy", "qty": 2})
    wait_for(client, lambda s: positions(s)[F1].get("NQ") == 2)
    client.post("/api/sim/price", json={"symbol": "NQ", "move": -10})  # F1: -400
    wait_for(client, lambda s: account(s, F1)["risk"]["locked"] and positions(s)[F1] == {})


def test_status_reports_trading_day_and_rules(client: TestClient):
    status = client.get("/api/status").json()
    assert len(status["trading_day"]) == 10 and status["next_reset"] > status["server_time"]
    risk = account(status, F1)["risk"]
    assert risk["daily_loss_limit"] == 2000 and risk["max_position"] == 10
    assert risk["locked"] is False and risk["loss_used"] == 0.0 and risk["in_trading_hours"] is True
    assert "risk" not in account(status, LEADER)


def test_daily_loss_breach_lock_and_manual_re_enable(client: TestClient):
    breach_f1(client)
    status = wait_for(client, lambda s: account(s, F1)["copy"]["sync_status"] == "risk_locked")
    assert positions(status)[F3] == {"MNQ": 20}  # the others are untouched
    assert "Daily loss limit $300.00 reached" in account(status, F1)["risk"]["reason"]

    wait_risk_events(client, [(F1, "daily_loss_limit")])
    assert client.get(f"/api/accounts/{F1}/risk-events").json()[0]["rule"] == "daily_loss_limit"
    logs = wait_json(client, "/api/logs", lambda rows: rows, category="risk", level="error")
    assert "copying is disabled" in logs[0]["message"]

    # locked: the next leader trade is not copied to F1
    client.post("/api/sim/close", json={})
    client.post("/api/sim/order", json={"side": "buy", "qty": 1})
    status = wait_for(client, lambda s: positions(s)[F3].get("MNQ") == 10)
    assert positions(status)[F1] == {}

    client.post("/api/sim/close", json={})
    assert client.post(f"/api/accounts/{F1}/risk/unlock").status_code == 200
    assert client.post(f"/api/accounts/{F1}/risk/unlock").status_code == 409
    assert client.post(f"/api/accounts/{LEADER}/risk/unlock").status_code == 409
    wait_for(client, lambda s: account(s, F1)["risk"]["locked"] is False)
    client.post("/api/sim/order", json={"side": "buy", "qty": 1})
    status = wait_for(client, lambda s: positions(s)[F1].get("NQ") == 1)
    assert account(status, F1)["risk"]["loss_used"] == 0.0  # fresh allowance


def test_simulated_new_trading_day_unlocks_and_resets_pnl(client: TestClient):
    breach_f1(client)
    client.post("/api/sim/close", json={})
    status = wait_for(client, lambda s: positions(s)[LEADER] == {})
    assert account(status, F1)["realized_pnl"] == -400.0

    r = client.post("/api/sim/new-day")
    assert r.status_code == 200 and r.json()["trading_day"] == status["trading_day"]
    status = wait_for(client, lambda s: account(s, F1)["risk"]["locked"] is False)
    assert account(status, F1)["realized_pnl"] == 0.0
    assert account(status, F1)["balance"] == 49_600.0
    logs = wait_json(client, "/api/logs", lambda rows: rows, category="risk", search="New trading day")
    assert "started from the simulator" in logs[0]["message"]


def test_lock_survives_a_restart(settings):
    with TestClient(create_app(settings, FAST_ENGINE)) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
        wait_for(client, lambda s: all(a["connection"] == "connected" for a in s["accounts"]))
        breach_f1(client)
    with TestClient(create_app(settings, FAST_ENGINE)) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
        status = wait_for(client, lambda s: all(a["connection"] == "connected" for a in s["accounts"]))
        assert account(status, F1)["risk"]["locked"] is True
        assert account(status, F2)["risk"]["locked"] is False
        client.post("/api/sim/order", json={"side": "buy", "qty": 2})
        status = wait_for(client, lambda s: positions(s)[F3].get("MNQ") == 20)
        assert positions(status)[F1] == {}


def test_max_position_change_applies_to_an_open_position(client: TestClient):
    client.post("/api/sim/order", json={"side": "buy", "qty": 4})
    wait_for(client, lambda s: positions(s)[F1].get("NQ") == 4)
    set_rules(client, F1, max_position=1)
    status = wait_for(client, lambda s: positions(s)[F1].get("NQ") == 1)
    assert account(status, F1)["copy"]["targets"] == {"NQ": 1}
    client.post("/api/sim/order", json={"side": "buy", "qty": 1})  # leader 5, F1 stays at 1
    wait_for(client, lambda s: positions(s)[F3].get("MNQ") == 50)
    status = wait_for(client, lambda s: account(s, F1)["risk"]["triggered_today"].get("max_position"))
    assert positions(status)[F1] == {"NQ": 1}
    events = wait_risk_events(client, [(F1, "max_position")])
    assert "capped at 1" in events[0]["message"]


def test_trading_hours_rule_blocks_entries_and_is_lifted_live(client: TestClient):
    # No allowed weekday = always outside the window, whatever the clock says.
    set_rules(client, F1, trading_hours_enabled=True, trading_days=[])
    wait_for(client, lambda s: account(s, F1)["risk"]["in_trading_hours"] is False)
    client.post("/api/sim/order", json={"side": "buy", "qty": 2, "stop_loss": 24950})
    status = wait_for(client, lambda s: positions(s)[F3].get("MNQ") == 20
                      and account(s, F1)["copy"]["reduce_only"])
    assert positions(status)[F1] == {} and account(status, F1)["working_orders"] == 0
    assert account(status, F1)["copy"]["sync_status"] == "in_sync"
    skipped = wait_trades(client, lambda items: items, follower_id=F1, status="skipped")
    assert any("Outside allowed trading hours" in o["error"] for i in skipped for o in i["orders"])
    wait_risk_events(client, [(F1, "trading_hours")])

    set_rules(client, F1, trading_hours_enabled=False)
    client.post("/api/sim/close", json={})
    wait_for(client, lambda s: positions(s)[LEADER] == {} and not account(s, F1)["copy"]["reduce_only"])
    client.post("/api/sim/order", json={"side": "buy", "qty": 2})
    wait_for(client, lambda s: positions(s)[F1].get("NQ") == 2)


def test_risk_rule_validation(client: TestClient):
    base = client.get(f"/api/accounts/{F1}/risk-rules").json()
    assert client.put(f"/api/accounts/{F1}/risk-rules", json={**base, "daily_loss_limit": -5}).status_code == 422
    assert client.put(f"/api/accounts/{F1}/risk-rules", json={**base, "max_position": 0}).status_code == 422
    assert client.put(f"/api/accounts/{F1}/risk-rules", json={**base, "trading_days": [7]}).status_code == 422
    cleared = set_rules(client, F1, daily_loss_limit=None, max_position=None)
    assert cleared["daily_loss_limit"] is None
    status = wait_for(client, lambda s: account(s, F1)["risk"]["daily_loss_limit"] is None)
    assert account(status, F1)["risk"]["max_position"] is None
