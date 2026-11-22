"""Keycloak project groups -> XNAT collaborator role (HTTP mocked with respx)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scoutoss_indexer.xnat_access import (
    KeycloakDirectory,
    XnatAccess,
    keep_warm,
    run_forever,
    sync_once,
)

KC = "http://kc.test"
XNAT = "http://xnat.test"
ADMIN = f"{KC}/admin/realms/scoutoss"


def result_set(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ResultSet": {"Result": rows}}


def mock_keycloak(
    mock: respx.MockRouter, *, users: list[str], groups: dict[str, list[dict[str, Any]]]
) -> None:
    mock.post(f"{KC}/realms/scoutoss/protocol/openid-connect/token").respond(
        json={"access_token": "t", "expires_in": 300}
    )
    mock.get(f"{ADMIN}/users").respond(json=[{"username": u} for u in users])
    mock.get(f"{ADMIN}/group-by-path/projects").respond(json={"id": "root"})
    mock.get(f"{ADMIN}/groups/root/children").respond(
        json=[{"id": f"g-{name}", "name": name} for name in groups]
    )
    for name, members in groups.items():
        mock.get(f"{ADMIN}/groups/g-{name}/members").respond(json=members)


def mock_xnat(
    mock: respx.MockRouter, *, users: list[str], roles: dict[str, list[dict[str, str]]]
) -> None:
    mock.get(f"{XNAT}/xapi/users").respond(json=users)
    mock.get(f"{XNAT}/data/projects").respond(json=result_set([{"ID": p} for p in roles]))
    for project, rows in roles.items():
        mock.get(f"{XNAT}/data/projects/{project}/users").respond(json=result_set(rows))


@pytest.fixture
def directory() -> KeycloakDirectory:
    return KeycloakDirectory(KC, "scoutoss", "scoutoss-xnat", "secret")


@pytest.fixture
def xnat() -> XnatAccess:
    return XnatAccess(XNAT, "admin", "pw")


@respx.mock
def test_grants_members_with_an_account_and_revokes_removed_ones(
    directory: KeycloakDirectory, xnat: XnatAccess, respx_mock: respx.MockRouter
) -> None:
    mock_keycloak(
        respx_mock,
        users=["demo", "researcher", "viewer", "left"],
        groups={
            "P1": [
                {"username": "demo"},
                {"username": "Researcher"},  # Keycloak keeps case; XNAT logins are lower-case
                {"username": "never-logged-in"},
                {"username": "disabled", "enabled": False},
            ]
        },
    )
    mock_xnat(
        respx_mock,
        users=["admin", "scout_indexer", "demo", "researcher", "left", "disabled"],
        roles={
            "P1": [
                {"login": "scout_indexer", "GROUP_ID": "P1_member", "displayname": "Members"},
                {"login": "researcher", "GROUP_ID": "P1_owner", "displayname": "Owners"},
                {"login": "left", "GROUP_ID": "P1_collaborator", "displayname": "Collaborators"},
                {
                    "login": "localonly",
                    "GROUP_ID": "P1_collaborator",
                    "displayname": "Collaborators",
                },
            ]
        },
    )
    grant = respx_mock.put(f"{XNAT}/data/projects/P1/users/collaborator/demo").respond(200)
    revoke = respx_mock.delete(f"{XNAT}/data/projects/P1/users/collaborator/left").respond(200)

    result = sync_once(directory, xnat)

    # demo: member with an account and no role -> granted. researcher already has a role (kept).
    # never-logged-in has no XNAT account yet; disabled users get nothing.
    assert result.granted == [("P1", "demo")] and grant.called
    # left: a Keycloak user no longer in the group -> removed. localonly is unknown to Keycloak
    # and scout_indexer is not a collaborator: both untouched.
    assert result.revoked == [("P1", "left")] and revoke.called
    assert respx_mock.calls.last.request.headers["cookie"] == "JSESSIONID=test-session"


@respx.mock
def test_nothing_to_do_and_project_without_group(
    directory: KeycloakDirectory, xnat: XnatAccess, respx_mock: respx.MockRouter
) -> None:
    mock_keycloak(respx_mock, users=["demo"], groups={"P1": [{"username": "demo"}]})
    mock_xnat(
        respx_mock,
        users=["demo"],
        roles={
            "P1": [{"login": "demo", "GROUP_ID": "P1_collaborator"}],
            "OTHER": [],  # no Keycloak group for this project: nobody is granted
        },
    )
    result = sync_once(directory, xnat)
    assert result.granted == [] and result.revoked == []


@respx.mock
def test_missing_projects_group_grants_nothing(
    directory: KeycloakDirectory, xnat: XnatAccess, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(f"{KC}/realms/scoutoss/protocol/openid-connect/token").respond(
        json={"access_token": "t"}
    )
    respx_mock.get(f"{ADMIN}/group-by-path/projects").respond(404)
    respx_mock.get(f"{ADMIN}/users").respond(json=[{"username": "demo"}])
    mock_xnat(respx_mock, users=["demo"], roles={"P1": []})
    assert sync_once(directory, xnat).granted == []


@respx.mock
def test_keycloak_pagination(directory: KeycloakDirectory, respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{KC}/realms/scoutoss/protocol/openid-connect/token").respond(
        json={"access_token": "t", "expires_in": 300}
    )
    pages = [[{"username": f"u{i}"} for i in range(200)], [{"username": "last"}]]
    route = respx_mock.get(f"{ADMIN}/users").mock(
        side_effect=[httpx.Response(200, json=p) for p in pages]
    )
    names = directory.usernames()
    assert len(names) == 201 and "last" in names
    assert route.calls[1].request.url.params["first"] == "200"
    # the token is cached: one token request for both pages
    assert respx_mock.calls[0].request.url.path.endswith("/token")
    assert sum(c.request.url.path.endswith("/token") for c in respx_mock.calls) == 1


@respx.mock
def test_loop_survives_a_keycloak_outage_and_writes_heartbeat(
    directory: KeycloakDirectory, xnat: XnatAccess, respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    heartbeat = tmp_path / "ok"
    stop = threading.Event()
    calls = {"n": 0}

    def token(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)  # first cycle: Keycloak down
        return httpx.Response(200, json={"access_token": "t", "expires_in": 300})

    respx_mock.post(f"{KC}/realms/scoutoss/protocol/openid-connect/token").mock(side_effect=token)
    respx_mock.get(f"{ADMIN}/group-by-path/projects").respond(json={"id": "root"})
    respx_mock.get(f"{ADMIN}/groups/root/children").respond(json=[])
    respx_mock.get(f"{ADMIN}/users").respond(json=[])
    respx_mock.get(f"{XNAT}/xapi/users").respond(json=[])
    respx_mock.get(f"{XNAT}/xapi/siteConfig/uptime").respond(500)  # warm-up failing is not fatal

    def projects(_: httpx.Request) -> httpx.Response:
        stop.set()  # second cycle reached XNAT: end the loop after it
        return httpx.Response(200, json=result_set([]))

    respx_mock.get(f"{XNAT}/data/projects").mock(side_effect=projects)

    run_forever(directory, xnat, interval=0.01, heartbeat=heartbeat, stop=stop)
    assert calls["n"] == 2 and heartbeat.exists()


@respx.mock
def test_stale_token_is_dropped_on_401(
    directory: KeycloakDirectory, respx_mock: respx.MockRouter
) -> None:
    tokens = respx_mock.post(f"{KC}/realms/scoutoss/protocol/openid-connect/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "old", "expires_in": 900}),
            httpx.Response(200, json={"access_token": "new", "expires_in": 900}),
        ]
    )

    def users(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == "Bearer old":
            return httpx.Response(401)
        return httpx.Response(200, json=[{"username": "demo"}])

    respx_mock.get(f"{ADMIN}/users").mock(side_effect=users)
    assert directory.usernames() == {"demo"}
    assert tokens.call_count == 2


WARM = f"{XNAT}/data/projects/P1/subjects/scoutoss-warmup"


def mock_warm_up(mock: respx.MockRouter, *, assessor_status: int = 200) -> dict[str, respx.Route]:
    mock.get(f"{XNAT}/data/projects").respond(json=result_set([{"ID": "P1"}]))
    session = f"{WARM}/experiments/scoutoss-warmup-ct"
    return {
        "subject": mock.put(WARM).respond(201),
        "session": mock.put(session).respond(201),
        "lookup": mock.get(session).respond(json={"items": [{"data_fields": {"ID": "XNAT_E9"}}]}),
        "assessor": mock.put(f"{session}/assessors/scoutoss-warmup-qc").respond(assessor_status),
        "cleanup": mock.delete(WARM).respond(200),
    }


@respx.mock
def test_warm_up_runs_on_first_cycle_and_after_an_xnat_restart(
    xnat: XnatAccess, respx_mock: respx.MockRouter
) -> None:
    routes = mock_warm_up(respx_mock)
    respx_mock.get(f"{XNAT}/xapi/siteConfig/uptime").mock(
        side_effect=[
            httpx.Response(200, json={"minutes": "2", "seconds": "5.5"}),
            httpx.Response(200, json={"hours": "1", "minutes": "0", "seconds": "0"}),
            httpx.Response(200, json={"seconds": "40.0"}),  # uptime went down: XNAT restarted
        ]
    )
    uptime = keep_warm(xnat, None)
    assert uptime == 125.5 and routes["assessor"].call_count == 1
    body = routes["assessor"].calls.last.request.content.decode()
    # the document carries every element the QC pipeline writes, and the real session ID
    assert "<xnat:imageSession_ID>XNAT_E9</xnat:imageSession_ID>" in body
    assert "xnat:scanStatistics" in body and "<xnat:note>" in body
    uptime = keep_warm(xnat, uptime)
    assert uptime == 3600 and routes["assessor"].call_count == 1  # still up: nothing to do
    keep_warm(xnat, uptime)
    assert routes["assessor"].call_count == 2 and routes["cleanup"].call_count == 2


@respx.mock
def test_warm_up_removes_its_placeholder_even_when_the_insert_fails(
    xnat: XnatAccess, respx_mock: respx.MockRouter
) -> None:
    routes = mock_warm_up(respx_mock, assessor_status=500)
    respx_mock.get(f"{XNAT}/xapi/siteConfig/uptime").respond(json={"seconds": "3"})
    with pytest.raises(httpx.HTTPStatusError):
        keep_warm(xnat, None)
    assert routes["cleanup"].called


@respx.mock
def test_warm_up_cleans_up_when_the_session_lookup_fails(
    xnat: XnatAccess, respx_mock: respx.MockRouter
) -> None:
    routes = mock_warm_up(respx_mock)
    routes["lookup"].respond(406)  # XNAT refuses some Accept headers on this endpoint
    respx_mock.get(f"{XNAT}/xapi/siteConfig/uptime").respond(json={"seconds": "3"})
    with pytest.raises(httpx.HTTPStatusError):
        keep_warm(xnat, None)
    assert routes["cleanup"].called and not routes["assessor"].called
    assert "*/*" in routes["lookup"].calls.last.request.headers["accept"]
