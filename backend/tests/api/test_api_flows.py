import csv
import io

from fastapi.testclient import TestClient

from tests.api.conftest import F1, F2, F3, LEADER, account, positions, wait_for, wait_trades


def test_seed_data(client: TestClient):
    accounts = client.get("/api/accounts").json()
    assert [(a["name"], a["role"], a["broker_type"]) for a in accounts] == [
        ("Leader (sim)", "leader", "sim"),
        ("F1 - 1x NQ", "follower", "sim"),
        ("F2 - 0.5x NQ", "follower", "sim"),
        ("F3 - NQ to MNQ 10:1", "follower", "sim"),
    ]
    f3 = client.get(f"/api/accounts/{F3}/copy-settings").json()
    assert f3["mappings"] == [{"leader_symbol": "NQ", "follower_symbol": "MNQ", "ratio": 10.0}]
    assert client.get(f"/api/accounts/{F2}/copy-settings").json()["multiplier"] == 0.5


def test_trade_is_copied_and_logged(client: TestClient):
    r = client.post("/api/sim/order", json={"side": "buy", "qty": 2, "stop_loss": 24980, "take_profit": 25040})
    assert r.status_code == 200, r.text
    status = wait_for(client, lambda s: positions(s)[F3].get("MNQ") == 20)
    assert positions(status)[F1] == {"NQ": 2}
    assert positions(status)[F2] == {"NQ": 1}
    assert account(status, F1)["working_orders"] == 2  # mirrored SL + TP
    assert account(status, F1)["copy"]["last_latency_ms"] is not None

    items = wait_trades(client, lambda items: any(
        i["event"] and i["event"]["type"] == "order_filled" and len(i["orders"]) == 3 for i in items))
    fill = next(i for i in items if i["event"] and i["event"]["type"] == "order_filled")
    assert {o["follower_name"] for o in fill["orders"]} == {"F1 - 1x NQ", "F2 - 0.5x NQ", "F3 - NQ to MNQ 10:1"}
    assert all(o["status"] == "filled" and o["latency_ms"] is not None for o in fill["orders"])

    # filters
    only_f3 = client.get("/api/trades", params={"follower_id": F3, "action": "copy_fill"}).json()["items"]
    assert only_f3 and all(o["follower_id"] == F3 for i in only_f3 for o in i["orders"])
    assert client.get("/api/trades", params={"status": "error"}).json()["items"] == []

    # close at the take profit
    client.post("/api/sim/price", json={"symbol": "NQ", "price": 25040})
    status = wait_for(client, lambda s: all(not a["positions"] for a in s["accounts"]))
    assert account(status, F3)["realized_pnl"] == 1600.0


def test_csv_export(client: TestClient):
    client.post("/api/sim/order", json={"side": "sell", "qty": 1})
    wait_trades(client, lambda items: any(i["orders"] for i in items))
    r = client.get("/api/trades/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows and {"time_utc", "leader_event", "follower_name", "status", "latency_ms"} <= set(rows[0])
    assert any(row["follower_name"] == "F3 - NQ to MNQ 10:1" and row["symbol"] == "MNQ" for row in rows)


def test_pause_resume_and_kill_switch(client: TestClient):
    assert client.post("/api/copier/pause").json()["state"] == "paused"
    client.post("/api/sim/order", json={"side": "buy", "qty": 2})
    status = wait_for(client, lambda s: positions(s)[LEADER].get("NQ") == 2)
    assert positions(status)[F1] == {}
    assert client.post("/api/copier/resume").json()["state"] == "running"
    status = wait_for(client, lambda s: account(s, F1)["copy"]["sync_status"] == "waiting_for_leader_flat")
    assert positions(status)[F1] == {}  # does not jump into an open trade

    client.post("/api/sim/close", json={})
    client.post("/api/sim/order", json={"side": "buy", "qty": 2})
    wait_for(client, lambda s: positions(s)[F1].get("NQ") == 2)

    assert client.post("/api/copier/kill").json()["state"] == "killed"
    status = wait_for(client, lambda s: all(not a["positions"] for a in s["accounts"] if a["role"] == "follower"))
    assert positions(status)[LEADER] == {"NQ": 2}
    kill_logs = client.get("/api/logs", params={"category": "kill_switch"}).json()
    assert kill_logs and any("Kill switch" in entry["message"] for entry in kill_logs)


def test_flatten_single_account(client: TestClient):
    client.post("/api/sim/order", json={"side": "buy", "qty": 2})
    wait_for(client, lambda s: positions(s)[F1].get("NQ") == 2)
    assert client.post(f"/api/accounts/{F1}/flatten").status_code == 200
    status = wait_for(client, lambda s: positions(s)[F1] == {})
    assert positions(status)[F3] == {"MNQ": 20}


def test_failure_injection_and_reconcile(client: TestClient):
    r = client.post(f"/api/sim/accounts/{F1}/failures",
                    json={"action": "reject_next", "count": 1, "reason": "No margin"})
    assert r.json()["failures"]["reject_next"] == 1
    client.post("/api/sim/order", json={"side": "buy", "qty": 2})
    # rejected first, then fixed by the reconciliation loop (every 0.2 s in tests)
    wait_trades(client, lambda items: items, status="rejected")
    wait_for(client, lambda s: positions(s)[F1].get("NQ") == 2)
    reconcile_logs = client.get("/api/logs", params={"category": "reconcile", "account_id": F1}).json()
    assert any("Auto-correcting" in e["message"] for e in reconcile_logs)


def test_disconnect_injection_and_reconnect(client: TestClient):
    client.post(f"/api/sim/accounts/{F3}/failures", json={"action": "disconnect", "duration_s": 0.4})
    wait_for(client, lambda s: account(s, F3)["connection"] != "connected")
    client.post("/api/sim/order", json={"side": "sell", "qty": 1})
    wait_for(client, lambda s: account(s, F3)["connection"] == "connected"
             and positions(s)[F3].get("MNQ") == -10, timeout=8)


def test_copy_settings_update_applies_live(client: TestClient):
    client.post("/api/sim/order", json={"side": "buy", "qty": 4})
    wait_for(client, lambda s: positions(s)[F2].get("NQ") == 2)
    body = client.get(f"/api/accounts/{F2}/copy-settings").json()
    body.update(multiplier=1.0, mismatch_mode="auto_correct")
    r = client.put(f"/api/accounts/{F2}/copy-settings", json=body)
    assert r.status_code == 200, r.text
    wait_for(client, lambda s: positions(s)[F2].get("NQ") == 4)


def test_copy_settings_validation(client: TestClient):
    base = client.get(f"/api/accounts/{F1}/copy-settings").json()
    bad = {**base, "mappings": [{"leader_symbol": "NQ", "follower_symbol": "ES", "ratio": 1}]}
    r = client.put(f"/api/accounts/{F1}/copy-settings", json=bad)
    assert r.status_code == 422 and "different index" in r.text
    assert client.put(f"/api/accounts/{F1}/copy-settings", json={**base, "multiplier": 0}).status_code == 422
    assert client.put(f"/api/accounts/{LEADER}/copy-settings", json=base).status_code == 400


def test_account_crud(client: TestClient):
    r = client.post("/api/accounts", json={"name": "F4 - 2x MNQ", "role": "follower"})
    assert r.status_code == 201
    new_id = r.json()["id"]
    assert client.post("/api/accounts", json={"name": "f4 - 2x mnq"}).status_code == 409
    assert client.post("/api/accounts", json={"name": "Second leader", "role": "leader"}).status_code == 409

    settings = client.get(f"/api/accounts/{new_id}/copy-settings").json()
    settings.update(multiplier=2, mappings=[{"leader_symbol": "NQ", "follower_symbol": "MNQ", "ratio": 10}])
    assert client.put(f"/api/accounts/{new_id}/copy-settings", json=settings).status_code == 200
    wait_for(client, lambda s: account(s, new_id)["connection"] == "connected")

    client.post("/api/sim/order", json={"side": "buy", "qty": 1})
    wait_for(client, lambda s: positions(s)[new_id].get("MNQ") == 20)

    r = client.put(f"/api/accounts/{new_id}", json={"name": "F4 renamed"})
    assert r.json()["name"] == "F4 renamed"
    assert client.post(f"/api/accounts/{new_id}/test-connection").json()["ok"] is True

    # disabling removes it from the copier; a disabled account is not copied
    client.put(f"/api/accounts/{new_id}", json={"enabled": False})
    client.post("/api/sim/order", json={"side": "buy", "qty": 1})
    status = wait_for(client, lambda s: positions(s)[F1].get("NQ") == 2)
    assert positions(status)[new_id] == {"MNQ": 20}

    assert client.delete(f"/api/accounts/{new_id}").status_code == 204
    assert client.get(f"/api/accounts/{new_id}").status_code == 404


def test_switching_the_leader(client: TestClient):
    client.put(f"/api/accounts/{LEADER}", json={"enabled": False})
    status = wait_for(client, lambda s: s["copier"]["state"] == "no_leader")
    assert status["copier"]["note"] == "No enabled leader account"
    assert client.post("/api/sim/order", json={"side": "buy", "qty": 1}).status_code == 409
    client.put(f"/api/accounts/{LEADER}", json={"enabled": True})
    wait_for(client, lambda s: s["copier"]["state"] == "running")


def test_risk_rules_storage(client: TestClient):
    rules = client.get(f"/api/accounts/{F1}/risk-rules").json()
    assert rules["daily_loss_limit"] == 2000
    rules.update(daily_loss_limit=750, trading_hours_enabled=True, trading_start="09:30",
                 trading_end="11:30", trading_days=[0, 2, 4])
    r = client.put(f"/api/accounts/{F1}/risk-rules", json=rules)
    assert r.status_code == 200 and r.json()["trading_days"] == [0, 2, 4]
    assert client.put(f"/api/accounts/{F1}/risk-rules",
                      json={**rules, "trading_start": "25:00"}).status_code == 422
    assert client.get("/api/risk/events").json() == []


def test_simulator_endpoints(client: TestClient):
    r = client.post("/api/sim/order", json={"side": "buy", "qty": 1, "type": "limit", "limit_price": 24990})
    order_id = r.json()["id"]
    state = client.get("/api/sim/state").json()
    assert state["leader_id"] == LEADER and [o["id"] for o in state["leader_orders"]] == [order_id]
    assert client.post(f"/api/sim/orders/{order_id}/modify", json={"limit_price": 24980}).json()["limit_price"] == 24980
    wait_for(client, lambda s: account(s, F1)["working_orders"] == 1)
    client.post(f"/api/sim/orders/{order_id}/cancel")
    wait_for(client, lambda s: account(s, F1)["working_orders"] == 0)
    assert client.post(f"/api/sim/orders/{order_id}/cancel").status_code == 409

    assert client.post("/api/sim/price", json={"symbol": "NQ", "move": 12.3}).json()["price"] == 25012.25
    assert client.post("/api/sim/price", json={"symbol": "ES"}).status_code == 422
    assert client.post("/api/sim/walk", json={"running": False}).json()["random_walk"] is False
    r = client.post(f"/api/sim/accounts/{F2}/failures", json={"action": "latency", "latency_ms": 150})
    assert r.json()["failures"]["latency_ms"] == 150
    client.post(f"/api/sim/accounts/{F2}/failures", json={"action": "clear"})
    assert client.get(f"/api/accounts/{F1}/orders").json() == []


def test_system_log_filters(client: TestClient):
    logs = client.get("/api/logs", params={"category": "connection"}).json()
    assert logs and all(e["category"] == "connection" for e in logs)
    assert client.get("/api/logs", params={"level": "error"}).json() == []
    search = client.get("/api/logs", params={"search": "logged in"}).json()
    assert search and "logged in" in search[0]["message"]


def test_admin_panel_is_served_with_spa_fallback(tmp_path):
    from app.main import create_app
    from tests.api.conftest import FAST_ENGINE, make_settings

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>panel</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    with TestClient(create_app(make_settings(tmp_path), FAST_ENGINE, frontend_dist=dist)) as c:
        assert c.get("/").text == "<html>panel</html>"
        assert c.get("/accounts").text == "<html>panel</html>"  # browser route on reload
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert c.get("/api/does-not-exist").status_code == 404
        assert c.get("/../../etc/passwd").text == "<html>panel</html>"


def test_placeholder_without_built_panel(tmp_path):
    from app.main import create_app
    from tests.api.conftest import FAST_ENGINE, make_settings

    with TestClient(create_app(make_settings(tmp_path), FAST_ENGINE, frontend_dist=tmp_path / "nope")) as c:
        assert "TradeCopier API is running" in c.get("/").text
