"""Auto-run: pipelines launched on newly arrived sessions, once, paced and audited."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scoutoss_common.audit import AuditLogger
from scoutoss_common.xnat_pipelines import XnatPipelines
from scoutoss_indexer.auto_run import (
    ERROR,
    LAUNCHED,
    SKIPPED,
    MemoryState,
    Rule,
    auto_run_once,
    modality,
    parse_rules,
    run_forever,
    status,
)
from tests.support.fake_opensearch import FakeOpenSearch

XNAT = "http://xnat.test"
QC = "scoutoss-qc-session"
RULES = [Rule(QC)]


def session(exp: str, project: str = "P1", kind: str = "ct") -> dict[str, Any]:
    return {"ID": exp, "project": project, "xsiType": f"xnat:{kind}SessionData"}


def mock_sessions(mock: respx.MockRouter, rows: list[dict[str, Any]]) -> None:
    rows = [*rows, {"ID": "QC1", "project": "P1", "xsiType": "xnat:qcAssessmentData"}]
    mock.get(f"{XNAT}/data/experiments").respond(json={"ResultSet": {"Result": rows}})


def mock_wrappers(mock: respx.MockRouter, enabled_in: set[str]) -> respx.Route:
    def available(request: httpx.Request) -> httpx.Response:
        wrapper = {
            "wrapper-id": 7,
            "wrapper-name": QC,
            "wrapper-contexts": ["xnat:imageSessionData"],
            "enabled": request.url.params["project"] in enabled_in,
        }
        return httpx.Response(200, json=[wrapper])

    return mock.get(f"{XNAT}/xapi/commands/available").mock(side_effect=available)


def mock_launch(mock: respx.MockRouter, project: str = "P1") -> respx.Route:
    return mock.post(f"{XNAT}/xapi/projects/{project}/wrappers/7/root/session/launch").respond(
        json={"status": "success", "workflow-id": "1"}
    )


def launched_sessions(route: respx.Route) -> list[str]:
    return [json.loads(c.request.content)["session"].rsplit("/", 1)[1] for c in route.calls]


@pytest.fixture
def xnat() -> XnatPipelines:
    return XnatPipelines(XNAT, "scout_pipeline", "pw")


@pytest.fixture
def audit(fake_os: FakeOpenSearch) -> AuditLogger:
    return AuditLogger(fake_os, service="auto-run")


def test_rules_and_modality() -> None:
    assert parse_rules("") == [] and parse_rules("none") == []
    assert parse_rules(" a , b:ct+MR ,, a:PT ") == [Rule("a"), Rule("b", frozenset({"CT", "MR"}))]
    assert modality(session("E1", kind="mr")) == "MR"
    assert Rule("b", frozenset({"CT"})).applies_to(session("E1"))
    assert not Rule("b", frozenset({"CT"})).applies_to(session("E1", kind="mr"))


@respx.mock
def test_only_sessions_that_arrive_after_enabling_are_run_and_only_once(
    xnat: XnatPipelines, audit: AuditLogger, fake_os: FakeOpenSearch, respx_mock: respx.MockRouter
) -> None:
    mock_sessions(respx_mock, [session("OLD1"), session("OLD2")])
    wrappers, launch = mock_wrappers(respx_mock, {"P1"}), mock_launch(respx_mock)
    state, pauses = MemoryState(), []

    def cycle() -> Any:
        return auto_run_once(xnat, state, audit, RULES, sleep=pauses.append)

    first = cycle()
    assert (first.launched, first.skipped) == (0, 0) and not launch.called
    assert not wrappers.called  # nothing to launch: the Container Service is not asked

    mock_sessions(respx_mock, [session("OLD1"), session("OLD2"), session("NEW1"), session("NEW2")])
    second = cycle()
    assert second.launched == 2 and launched_sessions(launch) == ["NEW1", "NEW2"]
    assert pauses == [0.25]  # paced: a pause between launches, none before the first
    assert wrappers.call_count == 1  # looked up once per project and cycle
    records = fake_os.docs("audit")
    assert [(r["user"], r["action"], r["resource"], r["outcome"]) for r in records] == [
        ("service:auto-run", "pipeline_launch", "NEW1", "attempted"),
        ("service:auto-run", "pipeline_launch", "NEW2", "attempted"),
    ]

    assert cycle().launched == 0 and launch.call_count == 2  # never launched twice
    assert status(state, RULES) == {
        QC: {
            "enabled": True,
            LAUNCHED: 2,
            SKIPPED: 2,
            ERROR: 0,
            "sessions_launched": ["NEW1", "NEW2"],
        }
    }


@respx.mock
def test_existing_sessions_are_run_when_asked(
    xnat: XnatPipelines, audit: AuditLogger, respx_mock: respx.MockRouter
) -> None:
    mock_sessions(respx_mock, [session("OLD1")])
    mock_wrappers(respx_mock, {"P1"})
    launch = mock_launch(respx_mock)
    state = MemoryState()
    assert auto_run_once(xnat, state, audit, RULES, run_existing=True).launched == 1
    assert launched_sessions(launch) == ["OLD1"]


@respx.mock
def test_other_modalities_and_projects_without_the_pipeline_are_skipped(
    xnat: XnatPipelines, audit: AuditLogger, fake_os: FakeOpenSearch, respx_mock: respx.MockRouter
) -> None:
    mock_sessions(respx_mock, [])
    mock_wrappers(respx_mock, {"P1"})
    launch = mock_launch(respx_mock)
    state, rules = MemoryState(), [Rule(QC, frozenset({"CT"}))]
    auto_run_once(xnat, state, audit, rules)  # enabled while XNAT is empty

    mock_sessions(
        respx_mock, [session("CT1"), session("MR1", kind="mr"), session("CT2", project="P2")]
    )
    result = auto_run_once(xnat, state, audit, rules)
    assert (result.launched, result.skipped) == (1, 2) and launched_sessions(launch) == ["CT1"]
    assert state.rows[("MR1", QC)].detail == "modality not selected"
    assert state.rows[("CT2", QC)].detail == "pipeline not enabled in project"
    assert len(fake_os.docs("audit")) == 1  # skipped sessions are not launches
    assert auto_run_once(xnat, state, audit, rules).skipped == 0  # decided once


@respx.mock
def test_failed_launch_is_retried_up_to_the_limit(
    xnat: XnatPipelines, audit: AuditLogger, fake_os: FakeOpenSearch, respx_mock: respx.MockRouter
) -> None:
    mock_sessions(respx_mock, [])
    mock_wrappers(respx_mock, {"P1"})
    launch = respx_mock.post(f"{XNAT}/xapi/projects/P1/wrappers/7/root/session/launch")
    launch.respond(500)
    state = MemoryState()
    auto_run_once(xnat, state, audit, RULES)
    mock_sessions(respx_mock, [session("E1")])
    for _ in range(4):
        auto_run_once(xnat, state, audit, RULES, max_attempts=2)
    assert launch.call_count == 2 and state.rows[("E1", QC)].status == ERROR
    outcomes = [r["outcome"] for r in fake_os.docs("audit")]
    assert outcomes == ["attempted", "failure", "attempted", "failure"]
    launch.respond(json={"status": "success"})
    assert auto_run_once(xnat, state, audit, RULES, max_attempts=3).launched == 1


@respx.mock
def test_nothing_is_launched_without_an_audit_trail(
    xnat: XnatPipelines, audit: AuditLogger, fake_os: FakeOpenSearch, respx_mock: respx.MockRouter
) -> None:
    mock_sessions(respx_mock, [])
    mock_wrappers(respx_mock, {"P1"})
    launch = mock_launch(respx_mock)
    state = MemoryState()
    auto_run_once(xnat, state, audit, RULES)
    mock_sessions(respx_mock, [session("E1")])
    fake_os.fail_index.add("audit")
    assert auto_run_once(xnat, state, audit, RULES).launched == 0
    assert not launch.called and ("E1", QC) not in state.rows
    fake_os.fail_index.clear()
    assert auto_run_once(xnat, state, audit, RULES).launched == 1  # once the trail is back


def test_loop_survives_an_xnat_outage_and_writes_heartbeat(
    audit: AuditLogger, tmp_path: Path
) -> None:
    stop, heartbeat, calls = threading.Event(), tmp_path / "ok", []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(503)  # first cycle: XNAT down, no attempt used
        stop.set()
        return httpx.Response(200, json={"ResultSet": {"Result": [session("E1")]}})

    xnat = XnatPipelines(XNAT, "scout_pipeline", "pw", transport=httpx.MockTransport(handler))
    state = MemoryState()
    run_forever(xnat, state, audit, RULES, interval=0.01, heartbeat=heartbeat, stop=stop)
    assert len(calls) == 2 and heartbeat.exists()
    assert state.started(QC) and state.rows[("E1", QC)].status == SKIPPED
