from __future__ import annotations

import secrets
import time
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


async def get_db(runtime: Runtime = Depends(get_runtime)) -> AsyncIterator[AsyncSession]:
    async with runtime.sessionmaker() as session:
        yield session


def session_user(scope_session: dict, runtime: Runtime) -> str | None:
    sid = scope_session.get("sid")
    if not sid:
        return None
    expires = runtime.sessions.get(sid)
    if expires is None or expires < time.time():
        runtime.sessions.pop(sid, None)
        return None
    return scope_session.get("user")


def require_auth(request: Request, runtime: Runtime = Depends(get_runtime)) -> str:
    user = session_user(request.session, runtime)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")
    return user


def start_session(request: Request, runtime: Runtime, username: str) -> None:
    sid = secrets.token_urlsafe(32)
    runtime.sessions[sid] = time.time() + runtime.settings.session_max_age_s
    request.session.clear()
    request.session.update({"sid": sid, "user": username})


def end_session(request: Request, runtime: Runtime) -> None:
    sid = request.session.get("sid")
    if sid:
        runtime.sessions.pop(sid, None)
    request.session.clear()
