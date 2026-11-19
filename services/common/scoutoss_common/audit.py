"""Append-only audit trail in the OpenSearch ``audit`` index.

Each record is ``{user, action, resource, params_hash, ts, ip, service, trace_id,
outcome}``. ``user`` is the caller's pseudonym; raw parameters are never stored, only a
SHA-256 over their canonical JSON, so the trail cannot itself leak PHI. Documents are
written with ``op_type=create`` and random IDs: an existing record can never be
overwritten through this API. Failures raise :class:`AuditError` — callers fail closed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from scoutoss_common.tracing import current_trace_id

AUDIT_INDEX = "audit"
ACTIONS = frozenset(
    {
        "query",
        "export",
        "cohort_save",
        "cohort_read",
        "pipeline_launch",
        "reindex",
        "clinical_ingest",
        "login",
    }
)

AUDIT_MAPPING: dict[str, Any] = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "user": {"type": "keyword"},
            "action": {"type": "keyword"},
            "resource": {"type": "keyword"},
            "params_hash": {"type": "keyword"},
            "ts": {"type": "date"},
            "ip": {"type": "ip", "ignore_malformed": True},
            "service": {"type": "keyword"},
            "trace_id": {"type": "keyword"},
            "outcome": {"type": "keyword"},
        },
    },
}

log = structlog.get_logger(__name__)


class SupportsIndex(Protocol):
    def index(self, *args: Any, **kwargs: Any) -> Any: ...


class AuditError(RuntimeError):
    pass


def params_hash(params: Any) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class AuditLogger:
    def __init__(self, client: SupportsIndex, *, service: str, index: str = AUDIT_INDEX) -> None:
        self.client = client
        self.service = service
        self.index = index

    def record(
        self,
        *,
        user: str,
        action: str,
        resource: str,
        params: Any = None,
        ip: str | None = None,
        outcome: str = "success",
    ) -> dict[str, Any]:
        if action not in ACTIONS:
            raise ValueError(f"unknown audit action {action!r}")
        doc = {
            "user": user,
            "action": action,
            "resource": resource,
            "params_hash": params_hash(params),
            "ts": datetime.now(UTC).isoformat(),
            "ip": ip,
            "service": self.service,
            "trace_id": current_trace_id(),
            "outcome": outcome,
        }
        try:
            self.client.index(index=self.index, id=str(uuid.uuid4()), body=doc, op_type="create")
        except Exception as exc:
            log.error("audit write failed", action=action, error=type(exc).__name__)
            raise AuditError("audit trail unavailable") from exc
        log.info("audit", action=action, resource=resource, outcome=outcome)
        return doc
