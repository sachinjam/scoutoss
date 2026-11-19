"""Structured JSON logging (structlog) shared by every ScoutOSS service.

Every record carries ``ts, level, service, env, trace_id, msg``; request middleware binds
``user_id`` (pseudonym), ``endpoint``, ``latency_ms`` and ``status``. A final processor
removes PHI keys and redacts PHI-looking strings, so even a careless ``log.info(...,
patient_name=...)`` cannot leak. Standard-library loggers (uvicorn, httpx, opensearch)
are routed through the same pipeline.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, TextIO

import structlog
from opentelemetry import trace
from structlog.types import EventDict, Processor, WrappedLogger

from scoutoss_common.phi import PHI_KEYS, REDACTED, scrub_value

_CONFIGURED: dict[str, Any] = {}


def add_trace_context(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    else:
        event_dict.setdefault("trace_id", None)
    return event_dict


def scrub_phi(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict.keys()):
        if key.lower() in PHI_KEYS:
            event_dict[key] = REDACTED
        elif key not in ("ts", "level", "service", "env", "trace_id", "span_id"):
            event_dict[key] = scrub_value(event_dict[key])
    return event_dict


def _static_fields(service: str, env: str) -> Processor:
    def processor(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("env", env)
        return event_dict

    return processor


def _drop_color_message(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(
    service: str,
    *,
    env: str | None = None,
    level: str | None = None,
    stream: TextIO | None = None,
) -> None:
    """Configure structlog + stdlib logging to emit one JSON object per line."""
    env = env or os.getenv("ENV") or "dev"
    level_name = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    numeric = logging.getLevelName(level_name)
    if not isinstance(numeric, int):
        numeric = logging.INFO

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        _static_fields(service, env),
        add_trace_context,
        _drop_color_message,
        structlog.processors.format_exc_info,
        scrub_phi,
        structlog.processors.EventRenamer("msg"),
    ]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared[:-1],
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[structlog.stdlib.ExtraAdder(), *shared[:-1]],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.EventRenamer("msg"),
            structlog.processors.JSONRenderer(sort_keys=False, default=str),
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric)
    for noisy in ("uvicorn.access", "opensearch", "urllib3", "httpx", "httpcore", "pynetdicom"):
        logging.getLogger(noisy).setLevel(max(numeric, logging.WARNING))
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    _CONFIGURED.update(service=service, env=env, level=level_name)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger
