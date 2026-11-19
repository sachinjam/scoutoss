from __future__ import annotations

import json

import pytest

from scoutoss_copilot.heuristic import HeuristicPlanner, plan_for
from scoutoss_copilot.llm import Message
from scoutoss_copilot.plan import validate_plan


def filters(plan: dict, index: str) -> list[dict]:  # type: ignore[type-arg]
    step = next(s for s in plan["steps"] if s["index"] == index)
    return step["query"].get("bool", {}).get("filter", [])


def test_spec_cross_modal_question() -> None:
    plan = plan_for("patients under 85 with lung cancer AND CT chest")
    validate_plan(plan)
    study = filters(plan, "dicom_studies")
    assert {"terms": {"modalities": ["CT"]}} in study
    assert {"terms": {"body_parts": ["CHEST"]}} in study
    assert {"range": {"patient_age": {"lt": 85.0}}} in study
    clinical = filters(plan, "clinical_events")
    assert clinical == [{"terms": {"diagnosis_category": ["LUNG_CANCER"]}}]
    assert plan["combine"] == "intersect" and plan["subject"] == "patients"


@pytest.mark.parametrize(
    ("question", "expect"),
    [
        ("how many CT studies", {"modalities": ["CT"], "subject": "studies"}),
        (
            "female patients over 60 with MRI brain",
            {"modalities": ["MR"], "body_parts": ["HEAD"], "sex": "F", "age": {"gt": 60.0}},
        ),
        (
            "men aged 40-50 with chest x-ray",
            {"modalities": ["CR"], "sex": "M", "age": {"gte": 40.0, "lte": 50.0}},
        ),
        (
            "PET scans in 2022 by modality",
            {"modalities": ["PT"], "year": {"gte": 2022, "lte": 2022}, "charts": ["modality"]},
        ),
        (
            "Siemens CT after 2020 per year",
            {"manufacturers": ["SIEMENS"], "year": {"gt": 2020}, "charts": ["year"]},
        ),
        (
            "studies before 2021 between 30 and 40",
            {"year": {"lt": 2021}, "age": {"gte": 30.0, "lte": 40.0}},
        ),
        (
            "patients at least 18 by sex and age distribution",
            {"age": {"gte": 18.0}, "charts": ["sex", "age"]},
        ),
    ],
)
def test_question_parsing(question: str, expect: dict) -> None:  # type: ignore[type-arg]
    plan = plan_for(question)
    validate_plan(plan)
    f = filters(plan, "dicom_studies")
    flat = json.dumps(f)
    for key in ("modalities", "body_parts", "manufacturers"):
        if key in expect:
            assert {"terms": {key: expect[key]}} in f
    if "sex" in expect:
        assert {"term": {"patient_sex": expect["sex"]}} in f
    if "age" in expect:
        assert {"range": {"patient_age": expect["age"]}} in f
    if "year" in expect:
        assert {"range": {"study_year": expect["year"]}} in f
    if "subject" in expect:
        assert plan["subject"] == expect["subject"]
    for c in expect.get("charts", []):
        assert c in plan["charts"]
    assert flat


def test_diagnosis_only_puts_demographics_on_clinical() -> None:
    plan = plan_for("women with breast cancer under 50")
    assert [s["index"] for s in plan["steps"]] == ["clinical_events"]
    f = filters(plan, "clinical_events")
    assert {"term": {"patient_sex": "F"}} in f and {"range": {"patient_age": {"lt": 50.0}}} in f


def test_everything_and_follow_ups() -> None:
    assert plan_for("show me everything")["steps"][0]["query"] == {"match_all": {}}
    previous = {"cohort_id": "c-1"}
    follow = plan_for("of those, how many are female?", previous)
    assert follow["base_cohort_id"] == "c-1"
    assert {"term": {"patient_sex": "F"}} in follow["steps"][0]["query"]["bool"]["filter"]
    plain = plan_for("among them", previous)
    assert plain["steps"][0]["index"] == "dicom_patients"
    assert "base_cohort_id" not in plan_for("how many MR", previous)


def test_planner_protocol_reads_history() -> None:
    planner = HeuristicPlanner()
    first = json.loads(
        planner.complete([Message("system", "s"), Message("user", "QUESTION: lung cancer")])
    )
    assert first["steps"][0]["index"] == "clinical_events"
    history = [
        Message("system", "s"),
        Message("user", "QUESTION: CT"),
        Message("assistant", json.dumps({"cohort_id": "c9"})),
        Message("user", "QUESTION: of those, male?"),
    ]
    assert json.loads(planner.complete(history))["base_cohort_id"] == "c9"
    broken = [Message("assistant", "not json"), Message("user", "QUESTION: of those")]
    assert "base_cohort_id" not in json.loads(planner.complete(broken))
