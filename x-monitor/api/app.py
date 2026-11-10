"""Internal HTTP API: health check for Docker and a base for a future web dashboard."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from core.database import SessionFactory
from services.monitor import Monitor
from services.timeutils import utcnow
from services.x_client import XClient


def create_app(
    session_factory: SessionFactory, monitor: Monitor | None = None, x_client: XClient | None = None
) -> FastAPI:
    app = FastAPI(title="X Monitor", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> JSONResponse:
        body: dict[str, Any] = {"status": "ok"}
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
            body["database"] = "ok"
        except Exception as exc:  # noqa: BLE001
            body.update(status="error", database=type(exc).__name__)
        if monitor is not None and monitor.last_tick_at is not None:
            body["monitor_last_tick_seconds_ago"] = round((utcnow() - monitor.last_tick_at).total_seconds(), 1)
        if x_client is not None:
            body["x_param_dialect"] = x_client.dialect.name
            body["x_rate_limits"] = x_client.rate_limits.snapshot()
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    return app
