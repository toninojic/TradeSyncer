from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import end_session, get_runtime, require_auth, start_session
from app.api.schemas import LoginIn
from app.core.security import verify_password
from app.engine.events import LogCategory, LogLevel
from app.services.runtime import Runtime

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_FAILURES = 5
WINDOW_S = 300
LOCKOUT_S = 60


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _locked(runtime: Runtime, key: str) -> float:
    now = time.time()
    attempts = runtime.login_failures[key]
    while attempts and now - attempts[0] > WINDOW_S:
        attempts.popleft()
    if len(attempts) >= MAX_FAILURES:
        return max(0.0, attempts[-1] + LOCKOUT_S - now)
    return 0.0


@router.post("/login")
async def login(body: LoginIn, request: Request, runtime: Runtime = Depends(get_runtime)) -> dict:
    key = _client_key(request)
    wait = _locked(runtime, key)
    if wait > 0:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"Too many failed logins. Try again in {int(wait) + 1} s.")
    settings = runtime.settings
    user_ok = hmac.compare_digest(body.username, settings.admin_username)
    # always verify, so timing does not reveal whether the username was right
    password_ok = verify_password(settings.admin_password_hash, body.password)
    if not (user_ok and password_ok):
        runtime.login_failures[key].append(time.time())
        await runtime.log(LogLevel.WARNING, LogCategory.ENGINE, f"Failed login from {key}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password")
    runtime.login_failures.pop(key, None)
    start_session(request, runtime, settings.admin_username)
    await runtime.log(LogLevel.INFO, LogCategory.ENGINE, f"Admin logged in from {key}")
    return {"user": settings.admin_username}


@router.post("/logout")
async def logout(request: Request, runtime: Runtime = Depends(get_runtime)) -> dict:
    end_session(request, runtime)
    return {"ok": True}


@router.get("/me")
async def me(user: str = Depends(require_auth)) -> dict:
    return {"user": user}
