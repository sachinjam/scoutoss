"""Helpers for building query-plan JSON in tests."""

from __future__ import annotations

import json
from typing import Any


def plan_json(*steps: dict[str, Any], **extra: Any) -> str:
    return json.dumps({"steps": list(steps), **extra})
