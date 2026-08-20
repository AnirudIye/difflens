import time
import uuid
from http import HTTPStatus

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.logging_setup import setup_logging
from app.routers import ai_settings, auth, findings, health, repositories, reviews

log = structlog.get_logger()

# Where in the request the value came from. Pydantic puts it first in loc;
# it is context, not part of the field name the caller recognises.
_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})

# Long enough to name several bad fields, short enough that a hostile body
# full of unknown keys cannot turn one 422 into a payload
MAX_VALIDATION_MESSAGE_CHARS = 300


def _field_name(loc: tuple[object, ...]) -> str:
    parts = list(loc)
    if parts and parts[0] in _LOCATIONS:
        parts = parts[1:]
    return ".".join(str(part) for part in parts) or "body"


def cap(message: str, limit: int = MAX_VALIDATION_MESSAGE_CHARS) -> str:
    """Bound the sentence, whatever the request did to earn it.

    Today's models are small enough that nothing reaches the limit, which is
    exactly why this is a function with its own test: the cap has to still be
    there when a model with fifty fields arrives.
    """
    if len(message) <= limit:
        return message
    return message[: limit - 3] + "..."


def create_app() -> FastAPI:
    setup_logging(settings.environment)
    production = settings.environment == "production"
    app = FastAPI(
        title="DiffLens API",
        # The schema is a map of every route and payload shape. It is a
        # development convenience, not part of the product, and a public
        # deployment has no reason to publish one.
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )

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
        # Every response here is JSON. Saying so stops a browser guessing
        # otherwise for a payload that contains attacker-authored PR text.
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_envelope(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Give schema rejections the same envelope as every other error.

        FastAPI's own 422 body is a bare list under "detail", which the
        frontend cannot read, and it echoes the offending "input" back. That
        input is sometimes a half-typed API key, so it is dropped here rather
        than returned and written to the access log.
        """
        fields = [
            {"field": _field_name(error["loc"]), "message": error["msg"]} for error in exc.errors()
        ]
        message = cap("; ".join(f"{field['field']}: {field['message']}" for field in fields))
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": message or "The request did not match the expected shape",
                    "fields": fields,
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

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
