"""Execute a validated plan against OpenSearch and compute cohort statistics.

Each step is a read-only ``_search`` with a 5 s server-side timeout, a 5 s client timeout and
``_source`` restricted to non-identifying fields. One request returns at most ``PAGE_SIZE``
rows; a step that matches more is paged with ``search_after`` on a unique key until every row
is read or ``MAX_MEMBERS`` rows have been collected (the result is then flagged ``truncated``).
Step results are combined on ``patient_pseudonym``; cohort statistics are then aggregated over
the cohort's studies with one more read-only search.

``MAX_MEMBERS`` exists because cohorts are passed back to OpenSearch as ``terms`` filters, which
OpenSearch limits to 65,536 values. Beyond that size the join has to move into aggregations
(see docs/scaling.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from opentelemetry import trace

from scoutoss_common.catalog import (
    CLINICAL_INDEX,
    PATIENTS_INDEX,
    PROJECT_FIELD,
    SERIES_INDEX,
    STUDIES_INDEX,
)
from scoutoss_copilot.plan import MAX_ROWS, QueryPlan

log = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)

STUDY_FIELDS = [
    "study_uid",
    "patient_pseudonym",
    "project",
    "study_date",
    "modalities",
    "body_parts",
    "patient_age",
    "patient_sex",
    "study_description",
    "xnat_experiment_id",
    "xnat_session_label",
    "viewer_url",
    "series_count",
    "instance_count",
]


PAGE_SIZE = 5_000  # rows per request; must stay <= index.max_result_window (10,000)
MAX_MEMBERS = 50_000  # rows collected per step / ids per terms filter (OpenSearch allows 65,536)
# A unique keyword per index, used as the search_after sort key.
UNIQUE_KEY = {
    STUDIES_INDEX: "study_uid",
    SERIES_INDEX: "series_uid",
    PATIENTS_INDEX: "patient_pseudonym",
    CLINICAL_INDEX: "event_id",
}


class SearchClient(Protocol):
    def search(self, *args: Any, **kwargs: Any) -> Any: ...


class ExecutionError(RuntimeError):
    pass


@dataclass
class StepResult:
    index: str
    total: int  # rows matching the step's query
    pseudonyms: set[str]
    study_uids: set[str]
    executed: dict[str, Any]
    aggregations: dict[str, Any] | None = None
    rows: int = 0  # rows actually read (all pages); less than ``total`` means truncated


@dataclass
class CohortResult:
    pseudonyms: list[str]
    study_uids: list[str]
    steps: list[StepResult]
    stats: dict[str, Any] = field(default_factory=dict)
    studies: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    @property
    def patient_count(self) -> int:
        return len(self.pseudonyms)


class Executor:
    def __init__(
        self,
        client: SearchClient,
        *,
        timeout: float = 5.0,
        max_rows: int = MAX_ROWS,
        max_members: int = MAX_MEMBERS,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self.client = client
        self.timeout = timeout
        self.max_rows = max_rows  # largest ``size`` a plan step may ask for
        self.max_members = max_members  # rows read per step across all pages
        self.page_size = page_size  # rows per request

    def _search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        body = {**body, "timeout": f"{int(self.timeout * 1000)}ms", "track_total_hits": True}
        try:
            resp: dict[str, Any] = self.client.search(
                index=index, body=body, request_timeout=self.timeout
            )
        except Exception as exc:
            raise ExecutionError(f"search on {index} failed: {type(exc).__name__}") from exc
        if resp.get("timed_out"):
            raise ExecutionError(f"search on {index} exceeded {self.timeout}s")
        return resp

    @staticmethod
    def scoped(
        index: str, query: dict[str, Any], projects: frozenset[str] | None
    ) -> dict[str, Any]:
        """Wrap ``query`` with the caller's project (tenant) filter. ``None`` = unrestricted."""
        if projects is None:
            return query
        return {"bool": {"filter": [query, {"terms": {PROJECT_FIELD[index]: sorted(projects)}}]}}

    def run_step(
        self,
        step_index: int,
        plan: QueryPlan,
        restrict: set[str] | None,
        projects: frozenset[str] | None = None,
    ) -> StepResult:
        step = plan.steps[step_index]
        query = self.scoped(step.index, step.query, projects)
        if restrict is not None:
            query = {
                "bool": {
                    "filter": [
                        query,
                        {"terms": {"patient_pseudonym": sorted(restrict)[: self.max_members]}},
                    ]
                }
            }
        # a plan's ``size`` only says "rows wanted" (0 = aggregations only); every matching row
        # is read, PAGE_SIZE at a time, so the model cannot shrink or bloat the page size
        size = self.page_size if step.size else 0
        source = ["patient_pseudonym"] + (["study_uid"] if step.index == STUDIES_INDEX else [])
        body: dict[str, Any] = {
            "query": query,
            "size": size,
            "_source": source,
            "sort": [{UNIQUE_KEY[step.index]: "asc"}],
        }
        if step.aggs:
            body["aggs"] = step.aggs
        with tracer.start_as_current_span(
            "copilot.search", attributes={"index": step.index, "size": size}
        ) as span:
            resp = self._search(step.index, body)
            hits = list(resp.get("hits", {}).get("hits", []))
            total_obj = resp.get("hits", {}).get("total", {})
            total = int(
                total_obj.get("value", len(hits)) if isinstance(total_obj, dict) else total_obj
            )
            # a plan step with size 0 only wants its aggregations
            page = hits
            while size and len(page) == size and len(hits) < min(total, self.max_members):
                more = self._search(
                    step.index,
                    {k: v for k, v in body.items() if k != "aggs"}
                    | {"search_after": page[-1]["sort"]},
                )
                page = more.get("hits", {}).get("hits", [])
                hits.extend(page)
            hits = hits[: self.max_members]
            span.set_attribute("rows", len(hits))
        return StepResult(
            index=step.index,
            total=total,
            rows=len(hits),
            pseudonyms={
                h["_source"]["patient_pseudonym"]
                for h in hits
                if h.get("_source", {}).get("patient_pseudonym")
            },
            study_uids={
                h["_source"]["study_uid"] for h in hits if h.get("_source", {}).get("study_uid")
            },
            executed={"index": step.index, **body},
            aggregations=resp.get("aggregations"),
        )

    def execute(
        self,
        plan: QueryPlan,
        *,
        base_cohort: set[str] | None = None,
        projects: frozenset[str] | None = None,
    ) -> CohortResult:
        steps = [self.run_step(i, plan, base_cohort, projects) for i in range(len(plan.steps))]
        sets = [s.pseudonyms for s in steps]
        members = set.intersection(*sets) if plan.combine == "intersect" else set.union(*sets)
        if base_cohort is not None:
            members &= base_cohort
        study_steps = [s for s in steps if s.index == STUDIES_INDEX]
        truncated = any(plan.steps[i].size and s.rows < s.total for i, s in enumerate(steps))
        result = CohortResult(
            pseudonyms=sorted(members), study_uids=[], steps=steps, truncated=truncated
        )
        self._cohort_stats(
            result,
            restrict_studies={u for s in study_steps for u in s.study_uids} or None,
            projects=projects,
        )
        return result

    def _cohort_stats(
        self,
        result: CohortResult,
        restrict_studies: set[str] | None,
        projects: frozenset[str] | None = None,
    ) -> None:
        if not result.pseudonyms:
            result.stats = empty_stats()
            return
        filters: list[dict[str, Any]] = [
            {"terms": {"patient_pseudonym": result.pseudonyms[: self.max_members]}}
        ]
        if restrict_studies is not None:
            filters.append({"terms": {"study_uid": sorted(restrict_studies)[: self.max_members]}})
        if projects is not None:
            filters.append({"terms": {"project": sorted(projects)}})
        body = {
            "query": {"bool": {"filter": filters}},
            "size": 50,
            "_source": STUDY_FIELDS,
            "sort": [{"study_date": {"order": "desc", "missing": "_last"}}],
            "aggs": {
                "patients": {"cardinality": {"field": "patient_pseudonym"}},
                "modality": {"terms": {"field": "modalities", "size": 20}},
                "body_part": {"terms": {"field": "body_parts", "size": 20}},
                "sex": {
                    "terms": {"field": "patient_sex", "size": 5},
                    "aggs": {"patients": {"cardinality": {"field": "patient_pseudonym"}}},
                },
                "age": {"histogram": {"field": "patient_age", "interval": 10, "min_doc_count": 1}},
                "year": {"terms": {"field": "study_year", "size": 50}},
                "manufacturer": {"terms": {"field": "manufacturers", "size": 20}},
                "age_stats": {"stats": {"field": "patient_age"}},
            },
        }
        resp = self._search(STUDIES_INDEX, body)
        aggs = resp.get("aggregations", {})
        result.studies = [h["_source"] for h in resp.get("hits", {}).get("hits", [])]
        total = resp.get("hits", {}).get("total", {})
        result.study_uids = (
            sorted(restrict_studies)
            if restrict_studies
            else [s["study_uid"] for s in result.studies]
        )
        result.stats = {
            "patients": len(result.pseudonyms),
            "studies": int(total.get("value", 0) if isinstance(total, dict) else total),
            "modality": _buckets(aggs.get("modality")),
            "body_part": _buckets(aggs.get("body_part")),
            "sex": {
                str(b["key"]): int(b.get("patients", {}).get("value", b["doc_count"]))
                for b in aggs.get("sex", {}).get("buckets", [])
            },
            "age": {
                str(int(float(b["key"]))): int(b["doc_count"])
                for b in aggs.get("age", {}).get("buckets", [])
            },
            "year": dict(sorted(_buckets(aggs.get("year")).items())),
            "manufacturer": _buckets(aggs.get("manufacturer")),
            "age_mean": (aggs.get("age_stats") or {}).get("avg"),
            "diagnosis": self._diagnoses(result.pseudonyms, projects),
        }

    def _diagnoses(
        self, pseudonyms: list[str], projects: frozenset[str] | None = None
    ) -> dict[str, int]:
        try:
            resp = self._search(
                CLINICAL_INDEX,
                {
                    "size": 0,
                    "query": self.scoped(
                        CLINICAL_INDEX,
                        {"terms": {"patient_pseudonym": pseudonyms[: self.max_members]}},
                        projects,
                    ),
                    "aggs": {
                        "dx": {
                            "terms": {"field": "diagnosis_category", "size": 20},
                            "aggs": {"patients": {"cardinality": {"field": "patient_pseudonym"}}},
                        }
                    },
                },
            )
        except ExecutionError:
            return {}
        return {
            str(b["key"]): int(b.get("patients", {}).get("value", b["doc_count"]))
            for b in resp.get("aggregations", {}).get("dx", {}).get("buckets", [])
        }


def _buckets(agg: dict[str, Any] | None) -> dict[str, int]:
    return {str(b["key"]): int(b["doc_count"]) for b in (agg or {}).get("buckets", [])}


def empty_stats() -> dict[str, Any]:
    return {
        "patients": 0,
        "studies": 0,
        "modality": {},
        "body_part": {},
        "sex": {},
        "age": {},
        "year": {},
        "manufacturer": {},
        "age_mean": None,
        "diagnosis": {},
    }


def cohort_members_query(pseudonyms: list[str]) -> dict[str, Any]:
    """Read-only query reproducing a saved cohort (for exports and pipeline launches)."""
    return {"terms": {"patient_pseudonym": pseudonyms[:MAX_MEMBERS]}}


def paged_hits(
    client: SearchClient,
    index: str,
    body: dict[str, Any],
    *,
    limit: int = MAX_MEMBERS,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Every ``_source`` matching ``body`` (up to ``limit``), read ``PAGE_SIZE`` rows at a time
    with ``search_after``. Used for exports and pipeline launches, which need whole cohorts."""
    page_body = {**body, "size": PAGE_SIZE, "sort": [{UNIQUE_KEY[index]: "asc"}]}
    out: list[dict[str, Any]] = []
    while len(out) < limit:
        resp = client.search(index=index, body=page_body, request_timeout=timeout)
        page = resp.get("hits", {}).get("hits", [])
        out.extend(h["_source"] for h in page)
        if len(page) < PAGE_SIZE:
            break
        page_body = {**page_body, "search_after": page[-1]["sort"]}
    return out[:limit]


__all__ = [
    "MAX_MEMBERS",
    "PATIENTS_INDEX",
    "CohortResult",
    "ExecutionError",
    "Executor",
    "StepResult",
    "paged_hits",
]
