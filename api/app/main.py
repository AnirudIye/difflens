import time
import uuid
from http import HTTPStatus

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.logging_setup import setup_logging
from app.routers import ai_settings, auth, findings, health, repositories, reviews

log = structlog.get_logger()


def create_app() -> FastAPI:
    setup_logging(settings.environment)
    app = FastAPI(title="DiffLens API")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
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

    @app.exception_handler(StarletteHTTPException)
    async def error_envelope(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        extra: dict = {}
        if isinstance(exc.detail, dict):
            code = exc.detail.get("code", "error")
            message = exc.detail.get("message", "")
            # e.g. review_already_exists carries the id of the review that won
            extra = {k: v for k, v in exc.detail.items() if k not in ("code", "message")}
        else:
            code = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_")
            message = str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    **extra,
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
            headers=exc.headers,
        )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(repositories.router)
    app.include_router(reviews.router)
    app.include_router(ai_settings.router)
    app.include_router(findings.router)
    return app


app = create_app()
