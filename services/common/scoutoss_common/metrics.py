"""Prometheus metrics shared by every service (``http_requests_total`` and
``http_request_duration_seconds``) plus a helper that renders ``/metrics``."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.responses import Response

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled",
    ["service", "method", "endpoint", "status"],
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["service", "method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)


def metrics_response(registry: CollectorRegistry = REGISTRY) -> Response:
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
