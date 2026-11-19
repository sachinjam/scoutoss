from __future__ import annotations

from typing import Any

import pytest

from scoutoss_copilot.charts import SUPPORTED, chart_for, choose_charts
from scoutoss_copilot.executor import ExecutionError, Executor, cohort_members_query, empty_stats
from scoutoss_copilot.heuristic import plan_for
from scoutoss_copilot.plan import validate_plan
from tests.support.fake_opensearch import FakeOpenSearch


def run(os_: FakeOpenSearch, question: str, **kw: Any):  # type: ignore[no-untyped-def]
    return Executor(os_).execute(validate_plan(plan_for(question)), **kw)


def test_intersect_imaging_and_clinical(populated_os: FakeOpenSearch) -> None:
    result = run(populated_os, "patients under 85 with lung cancer AND CT chest")
    assert result.pseudonyms == ["SPA"]  # SPC is 90 (capped), SPD has no CT chest
    assert result.stats["studies"] == 2 and result.stats["modality"] == {"CT": 2}
    assert result.stats["diagnosis"] == {"LUNG_CANCER": 1}
    assert {s.index for s in result.steps} == {"dicom_studies", "clinical_events"}
    executed = result.steps[0].executed
    assert executed["size"] <= 10000 and executed["_source"] == ["patient_pseudonym", "study_uid"]
    search_calls = [c for c in populated_os.calls if c[0] == "search"]
    assert all(c[1]["body"]["timeout"] == "5000ms" for c in search_calls)
    assert all(c[1]["request_timeout"] == 5.0 for c in search_calls)


def test_union_and_stats(populated_os: FakeOpenSearch) -> None:
    plan = validate_plan(
        {
            "steps": [
                {"index": "dicom_studies", "query": {"term": {"modalities": "MR"}}},
                {
                    "index": "clinical_events",
                    "query": {"term": {"diagnosis_category": "LUNG_CANCER"}},
                },
            ],
            "combine": "union",
        }
    )
    result = Executor(populated_os).execute(plan)
    assert result.pseudonyms == ["SPA", "SPB", "SPC", "SPD"]
    assert result.stats["sex"] == {"M": 2, "F": 1} or result.stats["patients"] == 4


def test_base_cohort_restriction(populated_os: FakeOpenSearch) -> None:
    result = run(populated_os, "CT studies", base_cohort={"SPC", "SPE"})
    assert result.pseudonyms == ["SPC"]


def test_empty_result_and_age_cap(populated_os: FakeOpenSearch) -> None:
    empty = run(populated_os, "MR knee")
    assert empty.pseudonyms == [] and empty.stats == empty_stats()
    old = run(populated_os, "patients over 85")
    assert old.pseudonyms == ["SPC"] and old.stats["age"] == {"90": 1}


def test_truncation_flag(populated_os: FakeOpenSearch) -> None:
    ex = Executor(populated_os, max_rows=2)
    result = ex.execute(
        validate_plan(
            {"steps": [{"index": "dicom_studies", "query": {"match_all": {}}, "size": 2}]}
        )
    )
    assert result.truncated is True


def test_errors_and_timeouts() -> None:
    class Broken:
        def search(self, **_: Any) -> Any:
            raise ConnectionError("down")

    class Slow:
        def search(self, **_: Any) -> Any:
            return {"timed_out": True, "hits": {"hits": []}}

    plan = validate_plan({"steps": [{"index": "dicom_studies"}]})
    with pytest.raises(ExecutionError, match="failed"):
        Executor(Broken()).execute(plan)
    with pytest.raises(ExecutionError, match="exceeded"):
        Executor(Slow()).execute(plan)


def test_diagnosis_lookup_failure_is_tolerated(populated_os: FakeOpenSearch) -> None:
    ex = Executor(populated_os)
    original = ex._search

    def flaky(index: str, body: dict[str, Any]) -> dict[str, Any]:
        if index == "clinical_events":
            raise ExecutionError("x")
        return original(index, body)

    ex._search = flaky  # type: ignore[method-assign]
    result = ex.execute(validate_plan(plan_for("CT chest")))
    assert result.stats["diagnosis"] == {}
    assert cohort_members_query(["A"]) == {"terms": {"patient_pseudonym": ["A"]}}


STATS = {
    "modality": {"CT": 3, "MR": 1},
    "sex": {"F": 2, "M": 1, "U": 1},
    "age": {"60": 2, "70": 1, "90": 1},
    "body_part": {"CHEST": 3},
    "year": {"2021": 2, "2022": 1},
    "diagnosis": {"LUNG_CANCER": 2},
    "manufacturer": {"SIEMENS": 4},
}


@pytest.mark.parametrize("kind", SUPPORTED)
def test_every_chart_type(kind: str) -> None:
    chart = chart_for(kind, STATS)
    assert chart is not None and chart["id"] == kind
    spec = chart["spec"]
    assert spec["$schema"].endswith("vega-lite/v5.json")
    assert spec["data"]["values"]
    mark = spec["mark"]["type"]
    assert mark == {"sex": "arc", "year": "line"}.get(kind, "bar")
    if kind == "sex":
        assert {v["category"] for v in spec["data"]["values"]} == {"Female", "Male", "Unknown"}
        assert spec["mark"]["innerRadius"] > 0  # donut
    if kind == "age":
        assert spec["encoding"]["x"]["bin"]["binned"] is True
        assert spec["data"]["values"][-1] == {"bin": 90, "bin_end": 100, "count": 1}


def test_chart_selection_follows_intent() -> None:
    assert [c["id"] for c in choose_charts(["year"], STATS)] == ["year", "modality", "sex"]
    assert [c["id"] for c in choose_charts([], STATS)] == ["modality", "sex", "age"]
    assert chart_for("bogus", STATS) is None
    assert chart_for("modality", {"modality": {}}) is None
    assert choose_charts(["diagnosis"], {"diagnosis": {"X": 1}}, limit=1)[0]["id"] == "diagnosis"
