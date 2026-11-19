"""FastAPI glue: request-scoped logging + metrics middleware and standard endpoints."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.routing import Match

from scoutoss_common.metrics import HTTP_LATENCY, HTTP_REQUESTS, metrics_response

log = structlog.get_logger("scoutoss.http")

_QUIET = {"/health", "/ready", "/metrics"}


def _route_template(request: Request) -> str:
    for route in request.app.router.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return str(getattr(route, "path", request.url.path))
    return "unmatched"


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, service: str) -> None:
        super().__init__(app)
        self.service = service

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        structlog.contextvars.clear_contextvars()
        endpoint = _route_template(request)
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(
            endpoint=endpoint, method=request.method, request_id=request_id
        )
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        finally:
            elapsed = time.perf_counter() - start
            HTTP_REQUESTS.labels(self.service, request.method, endpoint, str(status)).inc()
            HTTP_LATENCY.labels(self.service, request.method, endpoint).observe(elapsed)
            if endpoint not in _QUIET:
                log.info(
                    "request",
                    status=status,
                    latency_ms=round(elapsed * 1000, 2),
                )


def install_observability(app: FastAPI, service: str) -> None:
    """Add the request middleware and a ``/metrics`` endpoint."""
    app.add_middleware(RequestContextMiddleware, service=service)  # type: ignore[arg-type]

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return metrics_response()


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
