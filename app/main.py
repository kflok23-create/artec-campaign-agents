"""FastAPI web service. Phase 0: `/health` only. The approval inbox arrives in Phase 4.

The web service never runs agents or publishes; it only reads state and writes approvals.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from db.session import make_engine
from services.config import Settings, get_settings

log = logging.getLogger("web")

DB_CHECK_SECONDS = 3


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or get_settings()
        app.state.settings = s
        app.state.engine = make_engine(s.database_url.get_secret_value(), pool_size=2)
        yield
        await app.state.engine.dispose()

    # No public API docs: nothing here is meant for anyone but the owner and Railway.
    app = FastAPI(
        title="Artec Campaign Agents",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        s: Settings = request.app.state.settings
        db = "ok"
        try:
            with anyio.fail_after(DB_CHECK_SECONDS):
                async with request.app.state.engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        except Exception as e:  # noqa: BLE001 - any failure means unhealthy
            db = "unreachable"
            log.warning(
                "health check: database unreachable", extra={"ctx": {"error": type(e).__name__}}
            )
        body = {
            "status": "ok" if db == "ok" else "degraded",
            "service": "web",
            "db": db,
            "commit": (s.railway_git_commit_sha or "local")[:7],
        }
        return JSONResponse(body, status_code=200 if db == "ok" else 503)

    return app


app = create_app()
