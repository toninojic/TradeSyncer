import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from starlette.websockets import WebSocketDisconnect

from app.cli import main as cli_main
from app.config import Settings
from app.core.security import (
    SECRETS,
    CredentialCipher,
    RedactingFilter,
    encode_hash_for_env,
    hash_password,
    verify_password,
)
from app.main import create_app

from tests.api.conftest import PASSWORD


def test_health_is_public_everything_else_needs_login(anon: TestClient):
    assert anon.get("/api/health").status_code == 200
    for path in ("/api/status", "/api/accounts", "/api/trades", "/api/logs", "/api/sim/state"):
        assert anon.get(path).status_code == 401, path
    assert anon.post("/api/copier/kill").status_code == 401


def test_login_and_logout(anon: TestClient):
    r = anon.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200
    assert "tc_session" in anon.cookies
    assert anon.get("/api/auth/me").json() == {"user": "admin"}
    anon.post("/api/auth/logout")
    assert anon.get("/api/status").status_code == 401


def test_stolen_cookie_is_useless_after_logout(anon: TestClient):
    anon.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    cookie = anon.cookies.get("tc_session")
    anon.post("/api/auth/logout")
    anon.cookies.set("tc_session", cookie)
    assert anon.get("/api/status").status_code == 401  # server-side session was removed


def test_wrong_password_and_lockout(anon: TestClient):
    for _ in range(5):
        r = anon.post("/api/auth/login", json={"username": "admin", "password": "nope"})
        assert r.status_code == 401
    r = anon.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 429  # locked even with the right password


def test_wrong_username(anon: TestClient):
    r = anon.post("/api/auth/login", json={"username": "root", "password": PASSWORD})
    assert r.status_code == 401


def test_password_hashing():
    hashed = hash_password("hunter2!!")
    assert hashed.startswith("$argon2")
    assert verify_password(hashed, "hunter2!!")
    assert not verify_password(hashed, "hunter3!!")
    assert not verify_password("", "x")


def test_b64_hash_in_env_is_decoded(tmp_path):
    hashed = hash_password("abcdefgh")
    s = Settings(admin_password_hash=encode_hash_for_env(hashed), session_secret="s", encryption_key="k")
    assert s.admin_password_hash == hashed


def test_cipher_roundtrip_and_wrong_key():
    cipher = CredentialCipher(CredentialCipher.generate_key())
    token = cipher.encrypt({"username": "u", "password": "p@ss-word-1"})
    assert "p@ss-word-1" not in token
    assert cipher.decrypt(token)["password"] == "p@ss-word-1"
    other = CredentialCipher(CredentialCipher.generate_key())
    with pytest.raises(ValueError):
        other.decrypt(token)


def test_log_redaction_filter():
    SECRETS.add("my-very-secret-value")
    record = logging.LogRecord("x", logging.INFO, __file__, 1,
                               "connecting with %s and password=hunter2 token: abc123", ("my-very-secret-value",), None)
    RedactingFilter().filter(record)
    message = record.getMessage()
    assert "my-very-secret-value" not in message
    assert "hunter2" not in message and "abc123" not in message
    assert "password=***" in message


def test_broker_credentials_encrypted_at_rest_and_never_returned(client: TestClient, settings, caplog):
    secret = "Sup3r-Secret-Tradovate-PW"
    with caplog.at_level(logging.DEBUG):
        r = client.post("/api/accounts", json={
            "name": "Tradovate demo", "broker_type": "tradovate", "role": "follower",
            "enabled": False, "credentials": {"username": "toni", "password": secret, "cid": "1234",
                                              "secret": "api-secret-xyz"},
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["has_credentials"] is True
        assert secret not in r.text and "credentials" not in body
        account_id = body["id"]
        # the stub never connects
        test = client.post(f"/api/accounts/{account_id}/test-connection").json()
        assert test["ok"] is False and "stub" in test["message"]
        listing = client.get("/api/accounts").text
        logs = client.get("/api/logs").text
    assert secret not in listing and secret not in logs
    assert secret not in caplog.text and "api-secret-xyz" not in caplog.text

    db_path = settings.database_url.split(":///", 1)[1]
    with create_engine(f"sqlite:///{db_path}").connect() as conn:
        stored = conn.execute(text("SELECT credentials_enc FROM accounts WHERE id=:i"),
                              {"i": account_id}).scalar_one()
    assert stored and secret not in stored
    assert CredentialCipher(settings.encryption_key).decrypt(stored)["password"] == secret


def test_websocket_requires_login(anon: TestClient):
    with pytest.raises(WebSocketDisconnect) as exc:
        with anon.websocket_connect("/ws") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_websocket_rejects_foreign_origin(client: TestClient):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert exc.value.code == 4403


def test_websocket_streams_snapshot_and_trades(client: TestClient):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert {a["name"] for a in first["data"]["accounts"]} >= {"Leader (sim)", "F1 - 1x NQ"}
        client.post("/api/sim/order", json={"side": "buy", "qty": 1})
        seen = set()
        for _ in range(50):
            seen.add(ws.receive_json()["type"])
            if {"leader_event", "follower_order", "snapshot"} <= seen:
                break
        assert {"leader_event", "follower_order"} <= seen


def test_app_refuses_to_start_without_env():
    with pytest.raises(RuntimeError, match="app.cli init"):
        create_app(Settings(admin_password_hash="", session_secret="", encryption_key="",
                            _env_file=None))


def test_cli_init_creates_working_env(tmp_path, capsys):
    env = tmp_path / ".env"
    assert cli_main(["init", "--env-file", str(env), "--username", "toni", "--password", "longpassword1"]) == 0
    s = Settings(_env_file=str(env))
    assert s.admin_username == "toni"
    assert verify_password(s.admin_password_hash, "longpassword1")
    CredentialCipher(s.encryption_key)  # valid Fernet key
    assert cli_main(["init", "--env-file", str(env), "--password", "x" * 10]) == 1  # no overwrite
