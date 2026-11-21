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


ALL_STUDIES = {"steps": [{"index": "dicom_studies", "query": {"match_all": {}}}]}


def test_steps_are_paged_past_one_request(populated_os: FakeOpenSearch) -> None:
    """A step matching more rows than one request returns is read completely, page by page."""
    one_page = Executor(populated_os).execute(validate_plan(ALL_STUDIES))
    populated_os.calls.clear()
    paged = Executor(populated_os, page_size=2).execute(validate_plan(ALL_STUDIES))
    assert paged.truncated is False
    assert paged.pseudonyms == one_page.pseudonyms and paged.study_uids == one_page.study_uids
    assert paged.stats["studies"] == one_page.stats["studies"] == 6
    step = paged.steps[0]
    assert step.rows == step.total == 6
    searches = [c[1]["body"] for c in populated_os.calls if c[1]["index"] == "dicom_studies"]
    pages = [b for b in searches if b.get("sort") == [{"study_uid": "asc"}]]
    # 6 rows in pages of 2: three requests, and no fourth because the total is already known
    assert len(pages) == 3 and "search_after" not in pages[0]
    assert all("search_after" in b and "aggs" not in b for b in pages[1:])
    # the plan's own size never becomes the page size
    tiny = {"steps": [{**ALL_STUDIES["steps"][0], "size": 1}]}
    assert Executor(populated_os).execute(validate_plan(tiny)).steps[0].rows == 6


def test_truncation_flag(populated_os: FakeOpenSearch) -> None:
    result = Executor(populated_os, max_members=2).execute(validate_plan(ALL_STUDIES))
    assert result.truncated is True and result.steps[0].rows == 2 and result.steps[0].total == 6
    result = Executor(populated_os, max_members=2, page_size=1).execute(validate_plan(ALL_STUDIES))
    assert result.truncated is True and result.steps[0].rows == 2
    assert (
        Executor(populated_os, max_members=6).execute(validate_plan(ALL_STUDIES)).truncated is False
    )


def test_paged_hits_reads_everything_up_to_the_limit(
    populated_os: FakeOpenSearch, monkeypatch: Any
) -> None:
    from scoutoss_copilot import executor

    monkeypatch.setattr(executor, "PAGE_SIZE", 4)
    body = {"query": {"match_all": {}}, "_source": ["study_uid"]}
    rows = executor.paged_hits(populated_os, "dicom_studies", body)
    assert sorted(r["study_uid"] for r in rows) == ["1.1", "1.2", "2.1", "3.1", "4.1", "5.1"]
    assert len(executor.paged_hits(populated_os, "dicom_studies", body, limit=5)) == 5


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
