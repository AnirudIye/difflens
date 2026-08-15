import time
import uuid

import structlog
from fastapi import FastAPI, Request

from app.config import settings
from app.logging_setup import setup_logging
from app.routers import health

log = structlog.get_logger()


def create_app() -> FastAPI:
    setup_logging(settings.environment)
    app = FastAPI(title="DiffLens API")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        response = await call_next(request)
        log.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )
        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(health.router)
    return app


app = create_app()
