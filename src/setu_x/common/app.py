"""Shared FastAPI application factory: request IDs, trace context, access logs,
consistent error envelopes, and liveness/readiness endpoints.

Error envelope (all non-2xx responses produced by SETU-X itself):
    {"error": {"code": str, "message": str, "request_id": str, "details": ... (optional)}}

Health semantics (mirrors Kubernetes probes):
    GET /health/live   -> 200 {"status": "alive", ...}   the process is running and can answer HTTP
    GET /health/ready  -> 200 {"status": "ready", ...}   config loaded and the service can serve
                          503 {"status": "not_ready", ...} otherwise
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from setu_x import __version__
from setu_x.common.config import ServiceSettings
from setu_x.common.ids import (
    make_traceparent,
    new_request_id,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    valid_request_id,
)

log = logging.getLogger("setu_x.http")

ReadinessCheck = Callable[[], dict[str, str]]  # returns {check_name: "ok" | "<problem>"}


def error_body(code: str, message: str, request_id: str | None, details: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details is not None:
        err["details"] = details
    return {"error": err}


def create_app(
    settings: ServiceSettings,
    *,
    title: str,
    readiness: ReadinessCheck | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(title=title, version=__version__, lifespan=lifespan)
    app.state.settings = settings
    identity = {
        "service": settings.service_name,
        "instance": settings.instance,
        "env": settings.env,
        "mode": settings.mode,
        "version": __version__,
    }
    app.state.identity = identity

    @app.middleware("http")
    async def correlation_and_access_log(request: Request, call_next):
        incoming = request.headers.get("x-request-id")
        request_id = incoming if valid_request_id(incoming) else new_request_id()
        parsed = parse_traceparent(request.headers.get("traceparent"))
        trace_id = parsed[0] if parsed else new_trace_id()
        span_id = new_span_id()
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        request.state.span_id = span_id
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 3)
            if not request.url.path.startswith("/health"):
                log.info(
                    "http_request",
                    extra={
                        "request_id": request_id,
                        "trace_id": trace_id,
                        "method": request.method,
                        "path": request.url.path,
                        "status": status,
                        "duration_ms": duration_ms,
                    },
                )
        response.headers["X-Request-ID"] = request_id
        response.headers["traceparent"] = make_traceparent(trace_id, span_id)
        response.headers["X-Served-By"] = f"{settings.instance}@{settings.env}"
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        details = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body("invalid_request", "request validation failed", _rid(request), details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, str(exc.detail), _rid(request)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled_error", extra={"request_id": _rid(request)})
        return JSONResponse(
            status_code=500, content=error_body("internal_error", "unexpected server error", _rid(request))
        )

    @app.get("/health/live", tags=["health"])
    async def live() -> dict:
        return {"status": "alive", **identity}

    @app.get("/health/ready", tags=["health"])
    async def ready():
        checks = readiness() if readiness else {"config": "ok"}
        ok = all(v == "ok" for v in checks.values())
        body = {"status": "ready" if ok else "not_ready", "checks": checks, **identity}
        return JSONResponse(status_code=200 if ok else 503, content=body)

    return app


def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)
