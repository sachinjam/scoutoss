"""Indexer HTTP API: health/readiness/metrics, re-index control, XNAT webhook, the index
schema (consumed by the copilot) and HL7 clinical-event ingest (called by Mirth)."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from scoutoss_common.audit import AuditError, AuditLogger
from scoutoss_common.auth import JWTValidator, Principal, make_auth_dependency, require_roles
from scoutoss_common.catalog import (
    CLINICAL_INDEX,
    DLQ_INDEX,
    EXAMPLE_VALUE_FIELDS,
    FIELD_DESCRIPTIONS,
    PATIENTS_INDEX,
    SERIES_INDEX,
    STUDIES_INDEX,
)
from scoutoss_common.web import client_ip, install_observability
from scoutoss_indexer import SERVICE_NAME
from scoutoss_indexer.clinical import ClinicalEvent, ClinicalIngestError, ClinicalIngestor
from scoutoss_indexer.cursor import CursorStore
from scoutoss_indexer.store import IndexStore
from scoutoss_indexer.worker import Worker

log = structlog.get_logger(__name__)


@dataclass
class IndexerDeps:
    store: IndexStore
    validator: JWTValidator
    audit: AuditLogger
    worker: Worker | None = None
    cursor: CursorStore | None = None
    clinical: ClinicalIngestor | None = None
    webhook_token: str | None = None
    ready_checks: tuple[Callable[[], bool], ...] = ()


class ReindexRequest(BaseModel):
    project: str | None = None
    experiment_ids: list[str] | None = Field(default=None, max_length=1000)


class WebhookEvent(BaseModel):
    experiment_id: str | None = Field(default=None, max_length=64)
    project: str | None = Field(default=None, max_length=64)
    event: str | None = Field(default=None, max_length=64)


class ClinicalBatch(BaseModel):
    events: list[ClinicalEvent] = Field(min_length=1, max_length=500)


def create_app(deps: IndexerDeps | None = None, *, build: bool = True) -> FastAPI:
    state: dict[str, IndexerDeps] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if deps is not None:
            state["deps"] = deps
        elif build:  # pragma: no cover - wiring for the real service
            from scoutoss_indexer.bootstrap import build_deps

            state["deps"] = build_deps()
        current = state["deps"]
        try:
            current.store.ensure_indices()
        except Exception as exc:
            log.error("could not ensure indices at startup", error=type(exc).__name__)
        if current.worker is not None:
            current.worker.start()
        yield
        if current.worker is not None:
            current.worker.stop()

    app = FastAPI(title="ScoutOSS Indexer", version="0.1.0", lifespan=lifespan)
    install_observability(app, SERVICE_NAME)

    def get_deps() -> IndexerDeps:
        return state["deps"]

    authenticate = make_auth_dependency(lambda: state["deps"].validator)
    any_user = require_roles(authenticate, "admin", "researcher", "viewer", "service")
    admin_only = require_roles(authenticate, "admin")
    service_only = require_roles(authenticate, "admin", "service")

    @app.get("/health")
    def health(d: IndexerDeps = Depends(get_deps)) -> dict[str, Any]:
        worker_ok = d.worker is None or d.worker.alive
        if not worker_ok:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "worker not running")
        return {"status": "ok", "service": SERVICE_NAME}

    @app.get("/ready")
    def ready(d: IndexerDeps = Depends(get_deps)) -> dict[str, Any]:
        checks: dict[str, bool] = {}
        try:
            checks["opensearch"] = bool(d.store.client.ping())
        except Exception:
            checks["opensearch"] = False
        for i, check in enumerate(d.ready_checks):
            try:
                checks[getattr(check, "__name__", f"check{i}")] = bool(check())
            except Exception:
                checks[getattr(check, "__name__", f"check{i}")] = False
        if not all(checks.values()):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"checks": checks})
        return {"status": "ready", "checks": checks}

    @app.post("/reindex")
    def reindex(
        body: ReindexRequest,
        request: Request,
        principal: Principal = Depends(admin_only),
        d: IndexerDeps = Depends(get_deps),
    ) -> dict[str, Any]:
        _audit(d, principal, "reindex", body.project or "*", body.model_dump(), request)
        reset = 0
        if d.cursor is not None:
            reset = d.cursor.reset(project=body.project, experiment_ids=body.experiment_ids)
        if d.worker is not None:
            source = d.worker.source
            if hasattr(source, "forget"):
                reset += source.forget()
            d.worker.trigger(set(body.experiment_ids) if body.experiment_ids else None)
        return {"status": "scheduled", "reset": reset}

    @app.post("/webhook/xnat", status_code=status.HTTP_202_ACCEPTED)
    def webhook(
        event: WebhookEvent,
        x_scout_webhook_token: str | None = Header(default=None),
        d: IndexerDeps = Depends(get_deps),
    ) -> dict[str, str]:
        if (
            not d.webhook_token
            or not x_scout_webhook_token
            or not hmac.compare_digest(d.webhook_token, x_scout_webhook_token)
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook token")
        if d.worker is not None:
            d.worker.trigger({event.experiment_id} if event.experiment_id else None)
        return {"status": "accepted"}

    @app.get("/schema")
    def schema(
        _: Principal = Depends(any_user), d: IndexerDeps = Depends(get_deps)
    ) -> dict[str, Any]:
        return build_schema(d.store)

    @app.get("/stats")
    def stats(
        _: Principal = Depends(any_user), d: IndexerDeps = Depends(get_deps)
    ) -> dict[str, Any]:
        counts = {
            name: d.store.count(name)
            for name in (SERIES_INDEX, STUDIES_INDEX, PATIENTS_INDEX, DLQ_INDEX, CLINICAL_INDEX)
        }
        worker = None
        if d.worker is not None:
            worker = {
                "cycles": d.worker.state.cycles,
                "last_ok": d.worker.state.last_ok,
                "last_error": d.worker.state.last_error,
            }
        return {"counts": counts, "worker": worker}

    @app.post("/clinical/events")
    def clinical_events(
        batch: ClinicalBatch,
        request: Request,
        principal: Principal = Depends(service_only),
        d: IndexerDeps = Depends(get_deps),
    ) -> dict[str, Any]:
        if d.clinical is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "clinical ingest disabled")
        _audit(
            d, principal, "clinical_ingest", "clinical_events", {"n": len(batch.events)}, request
        )
        try:
            result = d.clinical.ingest(batch.events)
        except ClinicalIngestError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        return {"status": "ok", **result}

    return app


def _audit(
    d: IndexerDeps, principal: Principal, action: str, resource: str, params: Any, request: Request
) -> None:
    try:
        d.audit.record(
            user=principal.pseudonym,
            action=action,
            resource=resource,
            params=params,
            ip=client_ip(request),
        )
    except AuditError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "audit unavailable") from exc


def build_schema(store: IndexStore) -> dict[str, Any]:
    """Field catalog plus live, de-identified example values for low-cardinality fields."""
    indices: dict[str, Any] = {}
    for index, fields in FIELD_DESCRIPTIONS.items():
        examples: dict[str, list[str]] = {}
        doc_count = 0
        try:
            if store.client.indices.exists(index=index):
                doc_count = int(store.client.count(index=index)["count"])
                agg_fields = EXAMPLE_VALUE_FIELDS.get(index, ())
                if agg_fields:
                    resp = store.client.search(
                        index=index,
                        body={
                            "size": 0,
                            "aggs": {f: {"terms": {"field": f, "size": 15}} for f in agg_fields},
                        },
                    )
                    for f in agg_fields:
                        buckets = resp.get("aggregations", {}).get(f, {}).get("buckets", [])
                        examples[f] = [str(b["key"]) for b in buckets]
        except Exception as exc:
            log.warning("schema introspection failed", index=index, error=type(exc).__name__)
        indices[index] = {"fields": fields, "examples": examples, "doc_count": doc_count}
    return {"indices": indices, "join_field": "patient_pseudonym"}
