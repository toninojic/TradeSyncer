"""FastAPI application: REST API, WebSocket and (from M3) the admin panel."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api import routes_accounts, routes_auth, routes_control, routes_logs, routes_sim, routes_ws
from app.config import REPO_DIR, Settings, get_settings
from app.core.logging_setup import configure_logging
from app.core.security import CredentialCipher
from app.db.base import create_all, make_engine, make_sessionmaker
from app.db.seed import seed_demo_data
from app.engine.config import EngineSettings
from app.services.runtime import Runtime

log = logging.getLogger(__name__)

FRONTEND_DIST = REPO_DIR / "frontend" / "dist"

SETUP_HINT = (
    "Missing settings: {missing}. Create the .env file first:\n"
    "    python -m app.cli init\n"
    "(run it in the backend folder; see README.md)"
)

PLACEHOLDER_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>TradeCopier</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:60px auto;padding:0 16px;line-height:1.5}
code{background:#f2f2f2;padding:2px 5px;border-radius:4px}</style></head><body>
<h1>TradeCopier API is running</h1>
<p>The admin panel arrives in milestone 3. Until then you can try the API:</p>
<ol><li>Open <a href="/docs">/docs</a></li>
<li>Run <code>POST /api/auth/login</code> with your admin username and password</li>
<li>Call any other endpoint, e.g. <code>GET /api/status</code> or <code>POST /api/sim/order</code></li></ol>
</body></html>"""


def create_app(
    settings: Settings | None = None,
    engine_settings: EngineSettings | None = None,
    frontend_dist: Path | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    missing = settings.missing()
    if missing:
        raise RuntimeError(SETUP_HINT.format(missing=", ".join(missing)))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings.log_level)
        db_engine = make_engine(settings.database_url)
        await create_all(db_engine)
        sessionmaker = make_sessionmaker(db_engine)
        if settings.seed_demo_data:
            async with sessionmaker() as session:
                if await seed_demo_data(session):
                    log.info("Seeded demo accounts (1 leader + 3 followers)")
        runtime = Runtime(settings, sessionmaker, CredentialCipher(settings.encryption_key),
                          engine_settings=engine_settings)
        app.state.runtime = runtime
        await runtime.start()
        log.info("TradeCopier ready on http://%s:%s", settings.host, settings.port)
        try:
            yield
        finally:
            await runtime.stop()
            await db_engine.dispose()

    app = FastAPI(title="TradeCopier", version="0.2.0", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="tc_session",
        max_age=settings.session_max_age_s,
        same_site="strict",
        https_only=False,  # served on localhost over plain HTTP
    )
    for module in (routes_auth, routes_control, routes_accounts, routes_logs, routes_sim, routes_ws):
        app.include_router(module.router)

    @app.get("/api/health", tags=["meta"])
    async def health() -> dict:
        return {"ok": True}

    _mount_frontend(app, frontend_dist or FRONTEND_DIST)
    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built admin panel (frontend/dist). Unknown paths return
    index.html so browser routes like /accounts work on reload."""
    index = dist / "index.html"
    if not index.exists():
        @app.get("/", include_in_schema=False)
        async def placeholder() -> HTMLResponse:
            return HTMLResponse(PLACEHOLDER_PAGE)
        return

    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith(("api/", "ws")) or path == "api":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and root in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
