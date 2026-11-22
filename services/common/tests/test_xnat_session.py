"""XNAT session login: one login, cookie afterwards, a fresh login when the session is gone."""

from __future__ import annotations

import httpx
import pytest
import respx

from scoutoss_common.xnat_session import XnatSessionAuth

XNAT = "http://xnat.test"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> httpx.Client:
    monkeypatch.setattr(XnatSessionAuth, "session_id", None)  # undo the suite-wide open session
    return httpx.Client(base_url=XNAT, auth=XnatSessionAuth(XNAT + "/", "svc", "secret"))


def test_logs_in_once_and_then_sends_only_the_cookie(
    client: httpx.Client, respx_mock: respx.MockRouter
) -> None:
    login = respx_mock.post(f"{XNAT}/data/JSESSION").respond(text="ABC123\n")
    data = respx_mock.get(f"{XNAT}/data/projects").respond(json={})
    for _ in range(3):
        assert client.get("/data/projects").status_code == 200
    assert login.call_count == 1
    assert login.calls[0].request.headers["Authorization"].startswith("Basic ")
    for call in data.calls:
        assert call.request.headers["Cookie"] == "JSESSIONID=ABC123"
        assert "Authorization" not in call.request.headers


def test_logs_in_again_when_the_session_has_expired(
    client: httpx.Client, respx_mock: respx.MockRouter
) -> None:
    login = respx_mock.post(f"{XNAT}/data/JSESSION").mock(
        side_effect=[httpx.Response(200, text="OLD"), httpx.Response(200, text="NEW")]
    )
    data = respx_mock.get(f"{XNAT}/data/projects").mock(
        side_effect=[httpx.Response(200), httpx.Response(401), httpx.Response(200, json=[1])]
    )
    assert client.get("/data/projects").status_code == 200
    resp = client.get("/data/projects")  # 401 with the old session, retried with a new one
    assert resp.status_code == 200 and resp.json() == [1]
    assert login.call_count == 2
    assert data.calls[-1].request.headers["Cookie"] == "JSESSIONID=NEW"


def test_wrong_credentials_surface_as_an_error(
    client: httpx.Client, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(f"{XNAT}/data/JSESSION").respond(401)
    with pytest.raises(httpx.HTTPStatusError):
        client.get("/data/projects")
