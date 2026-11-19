"""Shared helpers for the one-shot bootstrap jobs (stdlib + httpx only)."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx


def log(job: str, msg: str, **fields: Any) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "level": "info",
        "service": f"bootstrap-{job}",
        "env": os.getenv("ENV", "dev"),
        "trace_id": None,
        "msg": msg,
        **fields,
    }
    print(json.dumps(record), flush=True)


def fail(job: str, msg: str, **fields: Any) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "level": "error",
        "service": f"bootstrap-{job}",
        "env": os.getenv("ENV", "dev"),
        "trace_id": None,
        "msg": msg,
        **fields,
    }
    print(json.dumps(record), flush=True)
    sys.exit(1)


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise SystemExit(f"missing environment variable {name}")
    return value


def wait_for(
    job: str, what: str, probe: Callable[[], bool], timeout: float = 900, every: float = 5
) -> None:
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            if probe():
                log(job, f"{what} is up", attempts=attempt)
                return
        except (httpx.HTTPError, OSError):
            pass
        if attempt % 12 == 1:
            log(job, f"waiting for {what}")
        time.sleep(every)
    fail(job, f"timed out waiting for {what}")
