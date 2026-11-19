"""The query plan the LLM must produce, and its validator.

A plan is one or more read-only OpenSearch searches whose matching patients are combined
(intersect / union) on ``patient_pseudonym`` — OpenSearch has no joins, so this is how a
question spanning imaging and clinical data ("lung cancer AND CT chest") is answered.
The plan JSON is exactly what the UI shows in the "Review Code" panel.

Validation is an allowlist, not a denylist: only known indices, known fields, known query
clauses and known aggregations pass. Anything that could mutate data (scripts,
``_update``, ``*_by_query``, delete) is rejected, ``size`` is capped at 10 000, and every
failure is counted in ``copilot_query_validation_failures_total{reason}``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from prometheus_client import Counter
from pydantic import BaseModel, Field, ValidationError, field_validator

from scoutoss_common.catalog import (
    CLINICAL_INDEX,
    PATIENTS_INDEX,
    SERIES_INDEX,
    STUDIES_INDEX,
    field_type,
)

VALIDATION_FAILURES = Counter(
    "copilot_query_validation_failures_total",
    "Generated queries rejected by the validator",
    ["reason"],
)

ALLOWED_INDICES = frozenset({STUDIES_INDEX, SERIES_INDEX, PATIENTS_INDEX, CLINICAL_INDEX})
MAX_ROWS = 10_000
MAX_TERMS_AGG = 1_000
MAX_DEPTH = 12

LEAF_QUERIES = frozenset(
    {"term", "terms", "range", "match", "match_phrase", "exists", "prefix", "wildcard", "ids"}
)
COMPOUND_QUERIES = frozenset({"bool", "match_all", "match_none", "constant_score"})
BOOL_KEYS = frozenset({"must", "filter", "should", "must_not", "minimum_should_match", "boost"})
AGGREGATIONS = frozenset(
    {
        "terms",
        "histogram",
        "date_histogram",
        "cardinality",
        "avg",
        "min",
        "max",
        "sum",
        "value_count",
        "stats",
        "filter",
        "range",
    }
)
AGG_PARAMS = frozenset(
    {
        "field",
        "size",
        "interval",
        "calendar_interval",
        "fixed_interval",
        "min_doc_count",
        "order",
        "ranges",
        "missing",
        "extended_bounds",
        "format",
        "precision_threshold",
        "keyed",
    }
)
# Words that indicate an attempt to mutate data or run code — never allowed anywhere.
FORBIDDEN = re.compile(
    r"(?i)(?<![a-z0-9])(_update\w*|_delete\w*|delete_by_query|update_by_query|_reindex|_bulk|"
    r"_doc|_create|_close|_open|_settings|_mapping|_snapshot|_ingest|_plugins|_security|"
    r"scripts?|script_score|script_fields|painless|stored_script)(?![a-z0-9])"
)
DENIED_KEYS = frozenset(
    {
        "script",
        "script_score",
        "script_fields",
        "runtime_mappings",
        "stored_fields",
        "profile",
        "explain",
        "rescore",
        "pit",
        "scroll",
        "delete",
        "update",
        "index",
        "upsert",
        "doc",
    }
)


class PlanValidationError(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class QueryStep(BaseModel):
    index: str
    query: dict[str, Any] = Field(default_factory=lambda: {"match_all": {}})
    aggs: dict[str, Any] | None = None
    size: int = Field(default=MAX_ROWS, ge=0)
    description: str | None = Field(default=None, max_length=300)


class QueryPlan(BaseModel):
    steps: list[QueryStep] = Field(min_length=1, max_length=4)
    combine: Literal["intersect", "union"] = "intersect"
    join_field: Literal["patient_pseudonym"] = "patient_pseudonym"
    subject: Literal["patients", "studies"] = "patients"
    charts: list[str] = Field(default_factory=list, max_length=4)
    base_cohort_id: str | None = Field(default=None, max_length=64)
    explanation: str | None = Field(default=None, max_length=500)

    @field_validator("charts")
    @classmethod
    def _known_charts(cls, v: list[str]) -> list[str]:
        known = {"modality", "sex", "age", "body_part", "year", "diagnosis", "manufacturer"}
        return [c for c in v if c in known]


def _fail(reason: str, detail: str) -> PlanValidationError:
    VALIDATION_FAILURES.labels(reason).inc()
    return PlanValidationError(reason, detail)


def extract_json(text: str) -> Any:
    """Parse the LLM output; tolerate ```json fences and leading prose."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1)
    elif not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise _fail("not_json", "no JSON object in model output")
        cleaned = cleaned[start : end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise _fail("not_json", f"invalid JSON: {exc.msg}") from exc


def _check_field(index: str, field: str, *, where: str) -> None:
    if not isinstance(field, str) or field_type(index, field) is None:
        raise _fail("unknown_field", f"field {field!r} is not queryable on {index} ({where})")


def _check_query(index: str, node: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise _fail("too_deep", "query nesting too deep")
    if isinstance(node, list):
        for item in node:
            _check_query(index, item, depth + 1)
        return
    if not isinstance(node, dict) or len(node) != 1:
        raise _fail("bad_query", "each query clause must be an object with one key")
    ((kind, spec),) = node.items()
    if kind in DENIED_KEYS or kind == "regexp" or kind == "query_string":
        raise _fail("forbidden_clause", f"{kind} is not allowed")
    if kind in ("match_all", "match_none"):
        return
    if kind == "bool":
        if not isinstance(spec, dict) or not set(spec) <= BOOL_KEYS:
            raise _fail("bad_query", "bool accepts must/filter/should/must_not only")
        for key in ("must", "filter", "should", "must_not"):
            if key in spec:
                _check_query(index, spec[key], depth + 1)
        return
    if kind == "constant_score":
        if not isinstance(spec, dict) or "filter" not in spec:
            raise _fail("bad_query", "constant_score needs a filter")
        _check_query(index, spec["filter"], depth + 1)
        return
    if kind not in LEAF_QUERIES:
        raise _fail("forbidden_clause", f"query type {kind!r} is not allowed")
    if not isinstance(spec, dict):
        raise _fail("bad_query", f"{kind} must be an object")
    if kind == "exists":
        _check_field(index, spec.get("field", ""), where="exists")
        return
    if kind == "ids":
        if not isinstance(spec.get("values"), list) or len(spec["values"]) > MAX_ROWS:
            raise _fail("bad_query", "ids.values must be a list of at most 10000")
        return
    fields = [k for k in spec if k != "boost"]
    if len(fields) != 1:
        raise _fail("bad_query", f"{kind} must target exactly one field")
    field = fields[0]
    _check_field(index, field, where=kind)
    value = spec[field]
    if kind == "terms" and (not isinstance(value, list) or len(value) > MAX_ROWS):
        raise _fail("bad_query", "terms needs a list of at most 10000 values")
    if kind == "wildcard":
        pattern = value.get("value") if isinstance(value, dict) else value
        if not isinstance(pattern, str) or pattern.startswith(("*", "?")):
            raise _fail("forbidden_clause", "leading wildcards are not allowed")
    if kind == "range" and (
        not isinstance(value, dict) or not set(value) <= {"gt", "gte", "lt", "lte", "format"}
    ):
        raise _fail("bad_query", "range accepts gt/gte/lt/lte only")


def _check_aggs(index: str, aggs: Any, depth: int = 0) -> None:
    if not isinstance(aggs, dict) or not aggs:
        raise _fail("bad_aggs", "aggs must be a non-empty object")
    if depth > 3:
        raise _fail("too_deep", "aggregations nested too deeply")
    for name, spec in aggs.items():
        if not isinstance(spec, dict):
            raise _fail("bad_aggs", f"aggregation {name!r} must be an object")
        sub = spec.get("aggs") or spec.get("aggregations")
        kinds = [k for k in spec if k not in ("aggs", "aggregations")]
        if len(kinds) != 1 or kinds[0] not in AGGREGATIONS:
            raise _fail("forbidden_agg", f"aggregation {kinds} is not allowed")
        params = spec[kinds[0]]
        if kinds[0] == "filter":
            _check_query(index, params)
        else:
            if not isinstance(params, dict) or not set(params) <= AGG_PARAMS:
                raise _fail("forbidden_agg", f"aggregation {name!r} has unsupported parameters")
            _check_field(index, params.get("field", ""), where=f"aggs.{name}")
            if int(params.get("size", 10)) > MAX_TERMS_AGG:
                raise _fail("size_cap", f"terms size above {MAX_TERMS_AGG}")
        if sub:
            _check_aggs(index, sub, depth + 1)


def validate_plan(raw: str | dict[str, Any], *, max_rows: int = MAX_ROWS) -> QueryPlan:
    data = extract_json(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise _fail("bad_plan", "plan must be a JSON object")
    blob = json.dumps(data)
    if FORBIDDEN.search(blob):
        raise _fail("mutation", "plan references a write/admin API or a script")
    if "query" in data and "steps" not in data:  # a bare search body -> one-step plan
        data = {"steps": [{"index": data.pop("index", STUDIES_INDEX), **data}]}
    try:
        plan = QueryPlan.model_validate(data)
    except ValidationError as exc:
        raise _fail("bad_plan", exc.errors()[0]["msg"]) from exc
    for step in plan.steps:
        if step.index not in ALLOWED_INDICES:
            raise _fail("unknown_index", f"index {step.index!r} is not allowed")
        if step.size > max_rows:
            raise _fail("size_cap", f"size {step.size} exceeds the {max_rows} row cap")
        _check_query(step.index, step.query)
        if step.aggs is not None:
            _check_aggs(step.index, step.aggs)
    return plan
