"""OpenTelemetry setup: W3C ``traceparent`` propagation, OTLP/HTTP export to Jaeger,
automatic spans for FastAPI routes and outbound httpx calls."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

if TYPE_CHECKING:
    from fastapi import FastAPI

_STATE: dict[str, bool] = {"httpx": False}


def setup_tracing(
    service: str,
    app: FastAPI | None = None,
    *,
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Install a tracer provider. Disabled when ``OTEL_SDK_DISABLED=true``.

    ``exporter`` lets tests capture spans in memory; otherwise spans go to
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` (default ``http://jaeger:4318``).
    """
    if os.getenv("OTEL_SDK_DISABLED", "false").lower() == "true" and exporter is None:
        return None
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service, "deployment.environment": os.getenv("ENV", "dev")}
        )
    )
    if exporter is None:  # pragma: no cover - exercised in the running stack
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318").rstrip("/")
        exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=provider, excluded_urls="health,ready,metrics"
        )
    if not _STATE["httpx"]:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
        _STATE["httpx"] = True
    return provider


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
