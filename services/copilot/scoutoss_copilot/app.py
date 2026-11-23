"""Copilot HTTP API. Every endpoint except health/ready/metrics requires a Keycloak JWT;
every query, cohort save, export and pipeline launch is written to the audit index."""

from __future__ import annotations

import csv
import io
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from scoutoss_common.audit import AuditError, AuditLogger
from scoutoss_common.auth import JWTValidator, Principal, make_auth_dependency, require_roles
from scoutoss_common.catalog import STUDIES_INDEX
from scoutoss_common.web import client_ip, install_observability
from scoutoss_common.xnat_pipelines import PipelineError, XnatPipelines
from scoutoss_copilot import SERVICE_NAME
from scoutoss_copilot.executor import MAX_MEMBERS, ExecutionError, paged_hits
from scoutoss_copilot.llm import LLMError, PHILeakError
from scoutoss_copilot.plan import PlanValidationError
from scoutoss_copilot.service import Copilot

log = structlog.get_logger(__name__)


@dataclass
class CopilotDeps:
    copilot: Copilot
    validator: JWTValidator
    audit: AuditLogger
    search: Any  # OpenSearch-like client (exports, pipeline lookups, readiness)
    pipelines: XnatPipelines | None = None
    max_pipeline_sessions: int = 200
    # Pause between launches. XNAT learns that a container finished by polling Docker's event
    # history, which only keeps the last 256 events; a burst of launches overflows it and the
    # results of some containers are never uploaded (31 of 148 in one unpaced run).
    pipeline_launch_interval: float = 0.25
    ready_checks: tuple[Callable[[], bool], ...] = field(default_factory=tuple)


class QueryRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    session_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )


class SaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class PipelineRequest(BaseModel):
    pipeline: str = Field(default="scoutoss-qc-session", min_length=1, max_length=128)


def create_app(deps: CopilotDeps | None = None, *, build: bool = True) -> FastAPI:
    state: dict[str, CopilotDeps] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if deps is not None:
            state["deps"] = deps
        elif build:  # pragma: no cover - wiring for the real service
            from scoutoss_copilot.bootstrap import build_deps

            state["deps"] = build_deps()
        yield

    app = FastAPI(title="ScoutOSS Copilot", version="0.1.0", lifespan=lifespan)
    install_observability(app, SERVICE_NAME)

    def get_deps() -> CopilotDeps:
        return state["deps"]

    authenticate = make_auth_dependency(lambda: state["deps"].validator)
    reader = require_roles(authenticate, "viewer", "researcher", "admin")
    researcher = require_roles(authenticate, "researcher", "admin")

    def audit(
        d: CopilotDeps,
        p: Principal,
        action: str,
        resource: str,
        params: Any,
        request: Request,
        outcome: str = "success",
    ) -> None:
        try:
            d.audit.record(
                user=p.pseudonym,
                action=action,
                resource=resource,
                params=params,
                ip=client_ip(request),
                outcome=outcome,
            )
        except AuditError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "audit trail unavailable"
            ) from exc

    def answer(
        d: CopilotDeps, p: Principal, body: QueryRequest, request: Request
    ) -> dict[str, Any]:
        scope = p.project_scope()
        if scope is not None and not scope:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "no project access")
        try:
            result = d.copilot.ask(
                body.question, user=p.pseudonym, session_id=body.session_id, projects=scope
            )
        except PlanValidationError as exc:
            audit(d, p, "query", "plan", {"q": body.question}, request, outcome="rejected")
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"error": "query_rejected", "reason": exc.reason, "detail": exc.detail},
            ) from exc
        except PHILeakError as exc:
            audit(d, p, "query", "plan", {"q": body.question}, request, outcome="phi_blocked")
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"error": "phi_detected", "detail": "remove identifiers from the question"},
            ) from exc
        except LLMError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": "llm_unavailable"}) from exc
        except ExecutionError as exc:
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT, {"error": "search_failed", "detail": str(exc)}
            ) from exc
        audit(
            d,
            p,
            "query",
            result.cohort_id or "none",
            {"q": result.question, "plan": result.generated_query},
            request,
        )
        return result.to_dict()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    @app.get("/ready")
    def ready(d: CopilotDeps = Depends(get_deps)) -> dict[str, Any]:
        checks: dict[str, bool] = {}
        try:
            checks["opensearch"] = bool(d.search.ping())
        except Exception:
            checks["opensearch"] = False
        for i, check in enumerate(d.ready_checks):
            name = getattr(check, "__name__", f"check{i}")
            try:
                checks[name] = bool(check())
            except Exception:
                checks[name] = False
        if not all(checks.values()):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"checks": checks})
        return {"status": "ready", "checks": checks, "llm_provider": d.copilot.provider}

    @app.post("/query")
    def query(
        body: QueryRequest,
        request: Request,
        p: Principal = Depends(reader),
        d: CopilotDeps = Depends(get_deps),
    ) -> dict[str, Any]:
        return answer(d, p, body, request)

    @app.post("/query/stream")
    def query_stream(
        body: QueryRequest,
        request: Request,
        p: Principal = Depends(reader),
        d: CopilotDeps = Depends(get_deps),
    ) -> StreamingResponse:
        """Server-sent events: ``status`` -> ``query`` -> ``token``* -> ``result`` (or ``error``)."""

        def sse(event: str, data: Any) -> str:
            return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

        def events() -> Iterator[str]:
            yield sse("status", {"stage": "planning"})
            try:
                result = answer(d, p, body, request)
            except HTTPException as exc:
                yield sse("error", {"status": exc.status_code, "detail": exc.detail})
                return
            yield sse("query", {"generated_query": result["generated_query"]})
            yield sse("status", {"stage": "answering"})
            for word in result["answer_text"].split(" "):
                yield sse("token", {"text": word + " "})
            yield sse("result", result)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/sessions/{session_id}/history")
    def history(
        session_id: str, p: Principal = Depends(reader), d: CopilotDeps = Depends(get_deps)
    ) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "turns": d.copilot.memory.history(p.pseudonym, session_id),
        }

    @app.delete("/sessions/{session_id}")
    def reset(
        session_id: str, p: Principal = Depends(reader), d: CopilotDeps = Depends(get_deps)
    ) -> dict[str, str]:
        d.copilot.memory.clear(p.pseudonym, session_id)
        return {"status": "cleared"}

    @app.get("/cohorts")
    def list_cohorts(
        p: Principal = Depends(reader), d: CopilotDeps = Depends(get_deps)
    ) -> dict[str, Any]:
        return {"cohorts": [c.summary() for c in d.copilot.cohorts.list(p.pseudonym)]}

    @app.get("/cohorts/{cohort_id}")
    def get_cohort(
        cohort_id: str,
        request: Request,
        p: Principal = Depends(reader),
        d: CopilotDeps = Depends(get_deps),
    ) -> dict[str, Any]:
        cohort = d.copilot.cohorts.get(cohort_id, p.pseudonym)
        if cohort is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "cohort not found")
        audit(d, p, "cohort_read", cohort_id, {}, request)
        return {**cohort.summary(), "members": cohort.members}

    @app.post("/cohorts/{cohort_id}/save")
    def save_cohort(
        cohort_id: str,
        body: SaveRequest,
        request: Request,
        p: Principal = Depends(researcher),
        d: CopilotDeps = Depends(get_deps),
    ) -> dict[str, Any]:
        audit(d, p, "cohort_save", cohort_id, body.model_dump(), request)
        cohort = d.copilot.cohorts.save(cohort_id, p.pseudonym, body.name)
        if cohort is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "cohort not found")
        return cohort.summary()

    @app.get("/cohorts/{cohort_id}/export")
    def export_cohort(
        cohort_id: str,
        request: Request,
        p: Principal = Depends(researcher),
        d: CopilotDeps = Depends(get_deps),
    ) -> StreamingResponse:
        cohort = d.copilot.cohorts.get(cohort_id, p.pseudonym)
        if cohort is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "cohort not found")
        audit(d, p, "export", cohort_id, {"format": "csv", "n": cohort.member_count}, request)
        studies = _cohort_studies(d, cohort.members, cohort.study_uids, p)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "patient_pseudonym",
                "study_uid",
                "study_date",
                "modalities",
                "body_parts",
                "patient_age",
                "patient_sex",
                "project",
                "xnat_experiment_id",
            ]
        )
        for s in studies:
            writer.writerow(
                [
                    s.get("patient_pseudonym"),
                    s.get("study_uid"),
                    s.get("study_date"),
                    "|".join(s.get("modalities") or []),
                    "|".join(s.get("body_parts") or []),
                    s.get("patient_age"),
                    s.get("patient_sex"),
                    s.get("project"),
                    s.get("xnat_experiment_id"),
                ]
            )
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="cohort-{cohort_id}.csv"'},
        )

    @app.get("/pipelines")
    def pipelines(
        project: str = "SCOUT_DEMO",
        p: Principal = Depends(researcher),
        d: CopilotDeps = Depends(get_deps),
    ) -> dict[str, Any]:
        if d.pipelines is None:
            return {"pipelines": []}
        try:
            return {"pipelines": d.pipelines.wrappers(project)}
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "XNAT unavailable") from exc

    @app.post("/cohorts/{cohort_id}/pipeline")
    def run_pipeline(
        cohort_id: str,
        body: PipelineRequest,
        request: Request,
        p: Principal = Depends(researcher),
        d: CopilotDeps = Depends(get_deps),
    ) -> dict[str, Any]:
        if d.pipelines is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "pipelines not configured")
        cohort = d.copilot.cohorts.get(cohort_id, p.pseudonym)
        if cohort is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "cohort not found")
        studies = [
            s
            for s in _cohort_studies(d, cohort.members, cohort.study_uids, p)
            if s.get("xnat_experiment_id") and s.get("project")
        ]
        if len(studies) > d.max_pipeline_sessions:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"cohort has {len(studies)} sessions; limit is {d.max_pipeline_sessions}",
            )
        audit(
            d,
            p,
            "pipeline_launch",
            cohort_id,
            {"pipeline": body.pipeline, "n": len(studies)},
            request,
        )
        launches, errors = [], []
        wrappers: dict[str, dict[str, Any]] = {}
        for n, s in enumerate(studies):
            if n and d.pipeline_launch_interval > 0:
                time.sleep(d.pipeline_launch_interval)
            project = s["project"]
            if project not in wrappers:
                try:
                    match = [
                        w
                        for w in d.pipelines.wrappers(project)
                        if body.pipeline in (w["name"], str(w["id"]))
                    ]
                except Exception as exc:
                    raise HTTPException(status.HTTP_502_BAD_GATEWAY, "XNAT unavailable") from exc
                if not match:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND,
                        f"pipeline {body.pipeline} not enabled in {project}",
                    )
                wrappers[project] = match[0]
            try:
                launches.append(
                    d.pipelines.launch(project, wrappers[project], s["xnat_experiment_id"])
                )
            except (PipelineError, Exception) as exc:
                errors.append(
                    {"experiment_id": s["xnat_experiment_id"], "error": type(exc).__name__}
                )
        log.info("pipeline launched on cohort", launched=len(launches), failed=len(errors))
        return {
            "cohort_id": cohort_id,
            "pipeline": body.pipeline,
            "launched": launches,
            "errors": errors,
        }

    return app


def _cohort_studies(
    d: CopilotDeps, members: list[str], study_uids: list[str], principal: Principal
) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = [{"terms": {"patient_pseudonym": members[:MAX_MEMBERS]}}]
    if study_uids:
        filters.append({"terms": {"study_uid": study_uids[:MAX_MEMBERS]}})
    scope = principal.project_scope()
    if scope is not None:  # tenancy is re-checked at export/launch time, not only at query time
        filters.append({"terms": {"project": sorted(scope)}})
    if not members:
        return []
    # whole cohorts are needed here (CSV export, pipeline launch): page past the 10,000-row window
    return paged_hits(
        d.search,
        STUDIES_INDEX,
        {
            "query": {"bool": {"filter": filters}},
            "_source": [
                "patient_pseudonym",
                "study_uid",
                "study_date",
                "modalities",
                "body_parts",
                "patient_age",
                "patient_sex",
                "project",
                "xnat_experiment_id",
            ],
        },
    )
