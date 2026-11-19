from __future__ import annotations

import json

import pytest

from scoutoss_copilot.plan import (
    VALIDATION_FAILURES,
    PlanValidationError,
    extract_json,
    validate_plan,
)


def step(**kw: object) -> dict[str, object]:
    return {"index": "dicom_studies", "query": {"match_all": {}}, **kw}


def test_valid_multi_step_plan() -> None:
    plan = validate_plan(
        json.dumps(
            {
                "steps": [
                    step(
                        query={
                            "bool": {
                                "filter": [
                                    {"term": {"modalities": "CT"}},
                                    {"range": {"patient_age": {"lt": 85}}},
                                    {"terms": {"body_parts": ["CHEST"]}},
                                    {"exists": {"field": "study_date"}},
                                    {"match": {"study_description": "chest"}},
                                    {"wildcard": {"study_description.raw": "CT*"}},
                                    {"prefix": {"project": "SCOUT"}},
                                ],
                                "must_not": [{"term": {"patient_sex": "U"}}],
                                "should": [{"match_phrase": {"study_description": "low dose"}}],
                            }
                        },
                        aggs={
                            "m": {
                                "terms": {"field": "modalities", "size": 10},
                                "aggs": {"p": {"cardinality": {"field": "patient_pseudonym"}}},
                            },
                            "f": {"filter": {"term": {"patient_sex": "F"}}},
                        },
                    ),
                    {
                        "index": "clinical_events",
                        "query": {
                            "constant_score": {
                                "filter": {"term": {"diagnosis_category": "LUNG_CANCER"}}
                            }
                        },
                    },
                ],
                "combine": "intersect",
                "charts": ["age", "bogus"],
            }
        )
    )
    assert len(plan.steps) == 2 and plan.charts == ["age"]


def test_bare_query_and_fences() -> None:
    plan = validate_plan(
        'Here you go:\n```json\n{"index": "dicom_patients", "query": {"match_all": {}}}\n```'
    )
    assert plan.steps[0].index == "dicom_patients"
    assert extract_json('prefix {"a": 1} suffix') == {"a": 1}
    assert validate_plan({"steps": [step()]}).steps[0].size == 10000


@pytest.mark.parametrize(
    ("plan", "reason"),
    [
        (
            '{"steps": [{"index": "dicom_studies", "query": {"match_all": {}}}], "op": "DELETE /dicom_studies/_delete_by_query"}',
            "mutation",
        ),
        (
            json.dumps({"steps": [step(query={"script": {"script": "ctx._source.x=1"}})]}),
            "mutation",
        ),
        (json.dumps({"steps": [step(index="dicom_studies/_update/1")]}), "mutation"),
        (json.dumps({"steps": [step(index="dicom_studies", extra="update_by_query")]}), "mutation"),
        (json.dumps({"steps": [step(index="audit")]}), "unknown_index"),
        (json.dumps({"steps": [step(index=".kibana")]}), "unknown_index"),
        (json.dumps({"steps": [step(size=10001)]}), "size_cap"),
        (json.dumps({"steps": [step(query={"term": {"patient_name": "x"}})]}), "unknown_field"),
        (json.dumps({"steps": [step(query={"regexp": {"modalities": ".*"}})]}), "forbidden_clause"),
        (json.dumps({"steps": [step(query={"query_string": {"query": "*"}})]}), "forbidden_clause"),
        (
            json.dumps({"steps": [step(query={"wildcard": {"study_description.raw": "*x"}})]}),
            "forbidden_clause",
        ),
        (
            json.dumps({"steps": [step(query={"more_like_this": {"like": "x"}})]}),
            "forbidden_clause",
        ),
        (json.dumps({"steps": [step(query={"term": {"a": 1}, "terms": {"b": [1]}})]}), "bad_query"),
        (json.dumps({"steps": [step(query={"bool": {"must": [], "nope": []}})]}), "bad_query"),
        (
            json.dumps(
                {"steps": [step(query={"range": {"patient_age": {"lt": 1, "boost_mode": 1}}})]}
            ),
            "bad_query",
        ),
        (json.dumps({"steps": [step(query={"terms": {"modalities": "CT"}})]}), "bad_query"),
        (
            json.dumps({"steps": [step(query={"term": {"modalities": "CT", "body_parts": "X"}})]}),
            "bad_query",
        ),
        (json.dumps({"steps": [step(query={"ids": {"values": "x"}})]}), "bad_query"),
        (json.dumps({"steps": [step(query={"constant_score": {}})]}), "bad_query"),
        (json.dumps({"steps": [step(query={"exists": {"field": "ssn"}})]}), "unknown_field"),
        (
            json.dumps(
                {"steps": [step(aggs={"x": {"terms": {"field": "modalities", "size": 5000}}})]}
            ),
            "size_cap",
        ),
        (json.dumps({"steps": [step(aggs={"x": {"top_hits": {"size": 5}}})]}), "forbidden_agg"),
        (
            json.dumps(
                {"steps": [step(aggs={"x": {"terms": {"field": "modalities", "include": ".*"}}})]}
            ),
            "forbidden_agg",
        ),
        (
            json.dumps({"steps": [step(aggs={"x": {"terms": {"field": "patient_name"}}})]}),
            "unknown_field",
        ),
        (json.dumps({"steps": [step(aggs={})]}), "bad_aggs"),
        (json.dumps({"steps": [step(aggs={"x": 1})]}), "bad_aggs"),
        (json.dumps({"steps": []}), "bad_plan"),
        ("[1, 2]", "not_json"),
        ("no json here", "not_json"),
        ('{"steps": [', "not_json"),
    ],
)
def test_rejections(plan: str, reason: str) -> None:
    before = VALIDATION_FAILURES.labels(reason)._value.get()
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert exc.value.reason == reason, exc.value.detail
    assert VALIDATION_FAILURES.labels(reason)._value.get() == before + 1


def test_deep_nesting_rejected() -> None:
    q: dict[str, object] = {"term": {"modalities": "CT"}}
    for _ in range(15):
        q = {"bool": {"filter": [q]}}
    with pytest.raises(PlanValidationError, match="too_deep"):
        validate_plan({"steps": [step(query=q)]})
    aggs: dict[str, object] = {"x": {"terms": {"field": "modalities"}}}
    for _ in range(5):
        aggs = {"x": {"terms": {"field": "modalities"}, "aggs": aggs}}
    with pytest.raises(PlanValidationError, match="too_deep"):
        validate_plan({"steps": [step(aggs=aggs)]})


def test_non_object_plan_and_custom_cap() -> None:
    with pytest.raises(PlanValidationError, match="bad_plan"):
        validate_plan('"just a string"' if False else {"steps": "x"})  # type: ignore[arg-type]
    with pytest.raises(PlanValidationError, match="size_cap"):
        validate_plan({"steps": [step(size=500)]}, max_rows=100)
    with pytest.raises(PlanValidationError, match="bad_plan"):
        validate_plan({"steps": [step(query=["not", "an object"])]})
    with pytest.raises(PlanValidationError, match="bad_query"):
        validate_plan({"steps": [step(query={"bool": {"filter": [["nested", "list"]]}})]})
