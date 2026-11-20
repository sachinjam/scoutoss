from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from scoutoss_common.audit import AuditLogger
from scoutoss_common.auth import JWTValidator
from scoutoss_copilot.app import CopilotDeps, create_app
from scoutoss_copilot.clients import XnatPipelines
from scoutoss_copilot.executor import ExecutionError
from scoutoss_copilot.heuristic import HeuristicPlanner
from scoutoss_copilot.llm import FakeLLM, LLMError
from tests.support.fake_opensearch import FakeOpenSearch

ISSUER = "https://auth.test/realms/scoutoss"
XNAT = "http://xnat.test"


@pytest.fixture
def deps(make_copilot: Any, populated_os: FakeOpenSearch, rsa_keys: dict[str, Any]) -> CopilotDeps:
    return CopilotDeps(
        copilot=make_copilot(HeuristicPlanner()),
        validator=JWTValidator(issuer=ISSUER, audience="scoutoss-copilot", jwks=rsa_keys["jwks"]),
        audit=AuditLogger(populated_os, service="copilot"),
        search=populated_os,
        pipelines=XnatPipelines(XNAT, "scout_pipeline", "pw"),
        max_pipeline_sessions=10,
        ready_checks=(lambda: True,),
    )


@pytest.fixture
def client(deps: CopilotDeps) -> Iterator[TestClient]:
    with TestClient(create_app(deps)) as c:
        yield c


def H(make_token: Callable[..., str], *roles: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(roles=roles or ('researcher',))}"}


def audit_actions(os_: FakeOpenSearch) -> list[tuple[str, str]]:
    return [(d["action"], d["outcome"]) for d in os_.docs("audit")]


def test_health_ready_metrics(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
    ready = client.get("/ready").json()
    assert ready["status"] == "ready" and ready["llm_provider"] == "heuristic"
    metrics = client.get("/metrics").text
    for name in (
        "copilot_queries_total",
        "copilot_llm_latency_seconds",
        "copilot_query_validation_failures_total",
        "http_requests_total",
        "http_request_duration_seconds",
    ):
        assert name in metrics


def test_ready_reports_failures(deps: CopilotDeps) -> None:
    def postgres() -> bool:
        raise ConnectionError("down")

    deps.ready_checks = (postgres,)
    with TestClient(create_app(deps)) as c:
        assert c.get("/ready").status_code == 503


def test_auth_required(client: TestClient, make_token: Callable[..., str]) -> None:
    body = {"question": "how many CT studies"}
    assert client.post("/query", json=body).status_code == 401
    expired = {"Authorization": f"Bearer {make_token(expires_in=-100)}"}
    assert client.post("/query", json=body, headers=expired).status_code == 401
    wrong_aud = {"Authorization": f"Bearer {make_token(audience='scoutoss-indexer')}"}
    assert client.post("/query", json=body, headers=wrong_aud).status_code == 401
    no_role = {"Authorization": f"Bearer {make_token(roles=('offline_access',))}"}
    assert client.post("/query", json=body, headers=no_role).status_code == 403


def test_query_response_schema_cohort_and_audit(
    client: TestClient, make_token: Callable[..., str], populated_os: FakeOpenSearch
) -> None:
    resp = client.post(
        "/query",
        json={"question": "patients under 85 with lung cancer AND CT chest", "session_id": "s-1"},
        headers=H(make_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    for key in (
        "answer_text",
        "cohort_count",
        "charts",
        "generated_query",
        "cohort_id",
        "executed_queries",
    ):
        assert key in data
    assert data["cohort_count"] == 1
    assert [s["index"] for s in data["generated_query"]["steps"]] == [
        "dicom_studies",
        "clinical_events",
    ]
    assert {c["id"] for c in data["charts"]} >= {"modality", "sex", "age"}
    assert audit_actions(populated_os) == [("query", "success")]
    cohorts = client.get("/cohorts", headers=H(make_token)).json()["cohorts"]
    assert cohorts[0]["id"] == data["cohort_id"] and cohorts[0]["member_count"] == 1
    history = client.get("/sessions/s-1/history", headers=H(make_token)).json()
    assert len(history["turns"]) == 1
    assert client.delete("/sessions/s-1", headers=H(make_token)).json()["status"] == "cleared"


def test_viewer_can_query_but_not_save_or_export(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    viewer = H(make_token, "viewer")
    cohort_id = client.post("/query", json={"question": "CT chest"}, headers=viewer).json()[
        "cohort_id"
    ]
    assert (
        client.post(f"/cohorts/{cohort_id}/save", json={"name": "x"}, headers=viewer).status_code
        == 403
    )
    assert client.get(f"/cohorts/{cohort_id}/export", headers=viewer).status_code == 403
    assert client.post(f"/cohorts/{cohort_id}/pipeline", json={}, headers=viewer).status_code == 403


def test_save_read_export(
    client: TestClient, make_token: Callable[..., str], populated_os: FakeOpenSearch
) -> None:
    h = H(make_token)
    cohort_id = client.post("/query", json={"question": "CT chest"}, headers=h).json()["cohort_id"]
    saved = client.post(
        f"/cohorts/{cohort_id}/save", json={"name": "CT chest cohort"}, headers=h
    ).json()
    assert saved["saved"] is True and saved["name"] == "CT chest cohort"
    detail = client.get(f"/cohorts/{cohort_id}", headers=h).json()
    assert sorted(detail["members"]) == ["SPA", "SPC"]
    export = client.get(f"/cohorts/{cohort_id}/export", headers=h)
    assert export.status_code == 200 and export.headers["content-type"].startswith("text/csv")
    rows = export.text.strip().splitlines()
    assert rows[0].startswith("patient_pseudonym,study_uid") and len(rows) == 4
    assert [a for a, _ in audit_actions(populated_os)] == [
        "query",
        "cohort_save",
        "cohort_read",
        "export",
    ]
    # other users cannot see it
    other = {"Authorization": f"Bearer {make_token(sub='someone-else')}"}
    assert client.get(f"/cohorts/{cohort_id}", headers=other).status_code == 404
    assert client.post("/cohorts/missing/save", json={"name": "x"}, headers=h).status_code == 404
    assert client.get("/cohorts/missing/export", headers=h).status_code == 404


def test_rejected_and_failed_queries(
    deps: CopilotDeps,
    make_copilot: Any,
    make_token: Callable[..., str],
    populated_os: FakeOpenSearch,
) -> None:
    deps.copilot = make_copilot(FakeLLM(default='{"steps": [{"index": "audit"}]}'))
    with TestClient(create_app(deps)) as c:
        resp = c.post("/query", json={"question": "show audit"}, headers=H(make_token))
        assert resp.status_code == 422 and resp.json()["detail"]["reason"] == "unknown_index"
        assert audit_actions(populated_os)[-1] == ("query", "rejected")

    class Boom(FakeLLM):
        def complete(self, messages: Any) -> str:
            raise LLMError("down")

    deps.copilot = make_copilot(Boom())
    with TestClient(create_app(deps)) as c:
        assert c.post("/query", json={"question": "x y"}, headers=H(make_token)).status_code == 502


def test_phi_block_and_search_failure(
    deps: CopilotDeps,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    populated_os: FakeOpenSearch,
) -> None:
    import scoutoss_copilot.llm as llm_mod
    import scoutoss_copilot.service as service_mod

    with TestClient(create_app(deps)) as c:
        # Both redaction layers broken -> the final leak check must still block the call.
        monkeypatch.setattr(llm_mod, "redact_text", lambda t, **_: t)
        monkeypatch.setattr(service_mod, "redact_text", lambda t, **_: t)
        resp = c.post("/query", json={"question": "studies for Jane Roe"}, headers=H(make_token))
        assert resp.status_code == 422 and resp.json()["detail"]["error"] == "phi_detected"
        assert audit_actions(populated_os)[-1] == ("query", "phi_blocked")
        monkeypatch.undo()

        def broken(*a: Any, **k: Any) -> Any:
            raise ExecutionError("search on dicom_studies exceeded 5.0s")

        monkeypatch.setattr(deps.copilot.executor, "execute", broken)
        assert (
            c.post("/query", json={"question": "CT chest"}, headers=H(make_token)).status_code
            == 504
        )


def test_audit_failure_fails_closed(
    client: TestClient, make_token: Callable[..., str], populated_os: FakeOpenSearch
) -> None:
    populated_os.fail_index.add("audit")
    assert (
        client.post("/query", json={"question": "CT chest"}, headers=H(make_token)).status_code
        == 503
    )


def test_streaming(client: TestClient, make_token: Callable[..., str]) -> None:
    with client.stream(
        "POST", "/query/stream", json={"question": "CT chest by sex"}, headers=H(make_token)
    ) as resp:
        body = "".join(resp.iter_text())
    events = [line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("event: ")]
    assert (
        events[0] == "status" and "query" in events and "token" in events and events[-1] == "result"
    )
    result = json.loads(body.strip().split("data: ")[-1])
    assert result["cohort_count"] == 2
    with client.stream(
        "POST", "/query/stream", json={"question": "zz"}, headers=H(make_token)
    ) as resp:
        assert "event: result" in "".join(resp.iter_text())


def test_stream_error_event(
    deps: CopilotDeps, make_copilot: Any, make_token: Callable[..., str]
) -> None:
    deps.copilot = make_copilot(FakeLLM(default="not json"))
    with (
        TestClient(create_app(deps)) as c,
        c.stream("POST", "/query/stream", json={"question": "x y"}, headers=H(make_token)) as resp,
    ):
        assert "event: error" in "".join(resp.iter_text())


def _mock_xnat(router: respx.MockRouter, enabled: str = "true") -> respx.Route:
    router.get(f"{XNAT}/xapi/commands/available").respond(
        json=[
            {
                "wrapper-id": 7,
                "wrapper-name": "scoutoss-qc-session",
                "wrapper-label": "QC",
                "enabled": enabled == "true",
                "wrapper-contexts": ["xnat:imageSessionData"],
            }
        ]
    )
    return router.post(f"{XNAT}/xapi/projects/SCOUT_DEMO/wrappers/7/root/session/launch").respond(
        json={"status": "success", "workflow-id": "w1"}
    )


def test_run_pipeline_on_cohort(
    client: TestClient, make_token: Callable[..., str], populated_os: FakeOpenSearch
) -> None:
    h = H(make_token)
    cohort_id = client.post("/query", json={"question": "CT chest"}, headers=h).json()["cohort_id"]
    with respx.mock(assert_all_called=False) as router:
        launch = _mock_xnat(router)
        listing = client.get("/pipelines", headers=h).json()
        assert [p["name"] for p in listing["pipelines"]] == ["scoutoss-qc-session"]
        resp = client.post(
            f"/cohorts/{cohort_id}/pipeline", json={"pipeline": "scoutoss-qc-session"}, headers=h
        )
        assert resp.status_code == 200
        out = resp.json()
        assert {x["experiment_id"] for x in out["launched"]} == {"E1", "E2", "E4"} and out[
            "errors"
        ] == []
        assert launch.call_count == 3
        missing = client.post(
            f"/cohorts/{cohort_id}/pipeline", json={"pipeline": "nope"}, headers=h
        )
        assert missing.status_code == 404
        launch.respond(500)
        partial = client.post(f"/cohorts/{cohort_id}/pipeline", json={}, headers=h).json()
        assert len(partial["errors"]) == 3
    assert ("pipeline_launch", "success") in audit_actions(populated_os)


def test_pipeline_limits_and_unconfigured(
    deps: CopilotDeps, make_token: Callable[..., str]
) -> None:
    deps.max_pipeline_sessions = 1
    with TestClient(create_app(deps)) as c:
        cid = c.post("/query", json={"question": "CT chest"}, headers=H(make_token)).json()[
            "cohort_id"
        ]
        assert c.post(f"/cohorts/{cid}/pipeline", json={}, headers=H(make_token)).status_code == 413
        assert c.post("/cohorts/nope/pipeline", json={}, headers=H(make_token)).status_code == 404
        with respx.mock() as router:
            router.get(f"{XNAT}/xapi/commands/available").respond(500)
            assert c.get("/pipelines", headers=H(make_token)).status_code == 502
    deps.pipelines = None
    with TestClient(create_app(deps)) as c:
        assert c.get("/pipelines", headers=H(make_token)).json() == {"pipelines": []}
        assert c.post("/cohorts/x/pipeline", json={}, headers=H(make_token)).status_code == 503


def test_input_validation(client: TestClient, make_token: Callable[..., str]) -> None:
    h = H(make_token)
    assert client.post("/query", json={"question": "x"}, headers=h).status_code == 422
    assert (
        client.post(
            "/query", json={"question": "ok q", "session_id": "../../etc"}, headers=h
        ).status_code
        == 422
    )


def test_project_tenancy(
    deps: CopilotDeps, make_token: Callable[..., str], populated_os: FakeOpenSearch
) -> None:
    populated_os.data["dicom_studies"]["1.1"]["project"] = (
        "OTHER"  # one of SPA's two CT chest studies
    )
    with TestClient(create_app(deps)) as c:
        mine = c.post("/query", json={"question": "CT chest"}, headers=H(make_token)).json()
        assert mine["study_count"] == 2  # 1.2 (SPA) + 3.1 (SPC); 1.1 belongs to OTHER
        executed = json.dumps(mine["executed_queries"])
        assert '"project": ["SCOUT_DEMO"]' in executed
        other = {"Authorization": f"Bearer {make_token(groups=('/projects/OTHER',), sub='o')}"}
        theirs = c.post("/query", json={"question": "CT chest"}, headers=other).json()
        assert theirs["study_count"] == 1 and theirs["cohort_count"] == 1
        admin = {"Authorization": f"Bearer {make_token(roles=('admin',), groups=(), sub='a')}"}
        assert (
            c.post("/query", json={"question": "CT chest"}, headers=admin).json()["study_count"]
            == 3
        )
        nobody = {"Authorization": f"Bearer {make_token(groups=(), sub='n')}"}
        assert c.post("/query", json={"question": "CT chest"}, headers=nobody).status_code == 403
        # an exported cohort is re-filtered by the caller's projects
        export = c.get(f"/cohorts/{theirs['cohort_id']}/export", headers=other)
        assert all("SCOUT_DEMO" not in row for row in export.text.splitlines()[1:])
