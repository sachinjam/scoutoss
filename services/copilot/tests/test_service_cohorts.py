from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
import pytest
import respx

from scoutoss_common.phi import redact_text
from scoutoss_copilot.clients import (
    PipelineError,
    SchemaClient,
    ServiceToken,
    XnatPipelines,
    static_schema,
)
from scoutoss_copilot.cohorts import ConversationMemory, MemoryCohortRepo, new_cohort
from scoutoss_copilot.heuristic import HeuristicPlanner
from scoutoss_copilot.llm import FakeLLM
from scoutoss_copilot.plan import PlanValidationError
from scoutoss_copilot.service import compose_answer
from tests.support.plans import plan_json


def test_ask_end_to_end_with_fake_llm(make_copilot: Any) -> None:
    fake = FakeLLM(
        default=plan_json(
            {"index": "dicom_studies", "query": {"term": {"modalities": "CT"}}},
            subject="studies",
            charts=["body_part"],
        )
    )
    repo = MemoryCohortRepo()
    copilot = make_copilot(fake, repo)
    answer = copilot.ask(
        "how many CT studies for John Doe SSN 123-45-6789", user="u_1", session_id="s1"
    )
    assert answer.cohort_count == 3 and answer.study_count == 4
    assert answer.answer_text.startswith("Found 4 studies from 3 patients")
    assert answer.generated_query["steps"][0]["query"] == {"term": {"modalities": "CT"}}
    assert next(c["id"] for c in answer.charts) == "body_part"
    assert answer.warnings and "123-45-6789" not in answer.question
    cohort = repo.get(answer.cohort_id or "", "u_1")
    assert (
        cohort is not None and cohort.member_count == 3 and cohort.query == answer.generated_query
    )
    assert "John Doe" not in str(fake.prompts) and "123-45-6789" not in str(fake.prompts)
    assert answer.studies and all("viewer_url" in s for s in answer.studies)
    assert answer.to_dict()["provider"] == "fake"


def test_follow_up_uses_memory_and_previous_cohort(make_copilot: Any) -> None:
    copilot = make_copilot(HeuristicPlanner())
    first = copilot.ask("CT chest", user="u_1", session_id="s")
    assert first.cohort_count == 2  # SPA, SPC
    follow = copilot.ask("of those, how many are female?", user="u_1", session_id="s")
    assert follow.cohort_count == 1 and follow.generated_query["base_cohort_id"] == first.cohort_id
    other_user = copilot.ask("of those, how many are female?", user="u_2", session_id="s")
    assert "base_cohort_id" not in other_user.generated_query  # memory is per user


# The redactor masks the leading 8-digit run of this UUID ("[ID]-c448-..."). About 1 in 40
# random cohort IDs look like this; sending real IDs to the LLM broke such follow-ups.
DIGIT_RUN_ID = "58239610-c448-40b6-a50d-1554bb2bd596"


def test_follow_up_survives_cohort_ids_the_redactor_would_mask(
    make_copilot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert DIGIT_RUN_ID not in redact_text(json.dumps({"cohort_id": DIGIT_RUN_ID}))
    real_uuid4 = uuid.uuid4
    ids = iter([uuid.UUID(DIGIT_RUN_ID)])
    monkeypatch.setattr(uuid, "uuid4", lambda: next(ids, None) or real_uuid4())
    copilot = make_copilot(HeuristicPlanner())
    first = copilot.ask("CT chest", user="u_1", session_id="s")
    assert first.cohort_id == DIGIT_RUN_ID
    follow = copilot.ask("of those, how many are female?", user="u_1", session_id="s")
    assert follow.warnings == []
    assert follow.cohort_count == 1 and follow.generated_query["base_cohort_id"] == DIGIT_RUN_ID


def test_missing_previous_cohort_warns(make_copilot: Any) -> None:
    fake = FakeLLM(default=plan_json({"index": "dicom_studies"}, base_cohort_id="gone"))
    answer = make_copilot(fake).ask("of those", user="u", session_id="s")
    assert any("not found" in w for w in answer.warnings)


def test_rejected_plan_propagates(make_copilot: Any) -> None:
    fake = FakeLLM(
        default='{"steps": [{"index": "dicom_studies", "query": {"match_all": {}}}], "x": "_delete_by_query"}'
    )
    with pytest.raises(PlanValidationError):
        make_copilot(fake).ask("delete everything", user="u", session_id="s")


def test_compose_answer_variants(make_copilot: Any) -> None:
    copilot = make_copilot(HeuristicPlanner())
    assert "No patients" in copilot.ask("MR knee", user="u", session_id="a").answer_text
    one = copilot.ask("MR brain", user="u", session_id="b")
    assert one.answer_text.startswith("Found 1 patient (1 imaging study)")
    assert compose_answer is not None


def test_cohort_repo_and_memory() -> None:
    repo = MemoryCohortRepo()
    c = repo.create(
        new_cohort(name="x" * 300, question="q", query={}, members=["A"], study_uids=[], user="u")
    )
    assert len(c.name) == 200 and repo.get(c.id, "other") is None
    assert repo.save(c.id, "u", "named").saved is True  # type: ignore[union-attr]
    assert repo.save("nope", "u", "n") is None and [x.id for x in repo.list("u")] == [c.id]
    assert c.summary()["member_count"] == 1
    mem = ConversationMemory(turns=2, max_sessions=2, ttl_seconds=0.05)
    for i in range(3):
        mem.add("u", "s", {"question": str(i)})
    assert [t["question"] for t in mem.history("u", "s")] == ["1", "2"]
    mem.add("u", "t", {}), mem.add("u", "v", {})
    assert mem.history("u", "s") == []  # evicted (LRU)
    time.sleep(0.06)
    assert mem.history("u", "v") == []  # expired
    mem.add("u", "w", {"q": 1})
    mem.clear("u", "w")
    assert mem.history("u", "w") == []


def test_service_token_and_schema_client() -> None:
    with respx.mock() as router:
        tok = router.post("http://kc/token").respond(
            json={"access_token": "abc", "expires_in": 300}
        )
        token = ServiceToken("http://kc/token", "scoutoss-copilot", "s")
        assert token.get() == "abc" and token.get() == "abc" and tok.call_count == 1
        live = {
            "indices": {
                "dicom_studies": {
                    "fields": {"modalities": "m"},
                    "examples": {"modalities": ["CT"]},
                    "doc_count": 7,
                },
                "dicom_series": {"doc_count": 9},
            }
        }
        schema_route = router.get("http://indexer/schema").respond(json=live)
        client = SchemaClient("http://indexer", token, ttl=60)
        schema = client.get()
        assert schema["indices"]["dicom_studies"]["examples"]["modalities"] == ["CT"]
        assert schema["indices"]["dicom_series"]["doc_count"] == 9
        assert schema_route.calls.last.request.headers["authorization"] == "Bearer abc"
        client.get()
        assert schema_route.call_count == 1  # cached
        schema_route.mock(side_effect=httpx.ConnectError("down"))
        fallback = SchemaClient("http://indexer", None, ttl=0).get()
        assert fallback == static_schema()


def test_xnat_pipelines_client() -> None:
    base = "http://xnat"
    with respx.mock() as router:
        router.get(f"{base}/xapi/commands/available").respond(
            json=[
                {
                    "wrapper-id": 7,
                    "wrapper-name": "scoutoss-qc-session",
                    "enabled": True,
                    "wrapper-contexts": ["xnat:imageSessionData"],
                    "image-name": "scoutoss/qc:0.1.0",
                },
                {
                    "wrapper-id": 9,
                    "wrapper-name": "disabled-one",
                    "enabled": False,
                    "wrapper-contexts": ["xnat:imageSessionData"],
                },
            ]
        )
        launch = router.post(f"{base}/xapi/projects/P/wrappers/7/root/session/launch").respond(
            json={"status": "success", "workflow-id": "123"}
        )
        client = XnatPipelines(base, "u", "p")
        wrappers = client.wrappers("P")
        assert [w["name"] for w in wrappers] == ["scoutoss-qc-session"]
        out = client.launch("P", wrappers[0], "E1")
        assert out == {
            "experiment_id": "E1",
            "status": "success",
            "workflow_id": "123",
            "container_id": None,
        }
        assert json.loads(launch.calls.last.request.content) == {
            "session": "/archive/experiments/E1"
        }
        with pytest.raises(PipelineError, match="session level"):
            client.launch("P", {"id": 8, "contexts": ["xnat:imageScanData"]}, "E1")
        launch.respond(500)
        with pytest.raises(PipelineError, match="HTTP 500"):
            client.launch("P", wrappers[0], "E1")
