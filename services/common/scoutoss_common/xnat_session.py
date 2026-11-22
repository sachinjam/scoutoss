"""Log in to XNAT once and reuse the session, instead of HTTP basic auth on every request.

XNAT opens a new login session for every request that carries basic credentials, keeps each one
for the idle timeout (15 minutes) and refuses the account with 401 once it has
``concurrentMaxSessions`` (1,000) open. A service that polls a few requests a second gets there
within minutes and then locks out every other user of the same account. This ``httpx`` auth
logs in with ``POST /data/JSESSION``, sends only the session cookie afterwards and logs in again
when XNAT answers 401 (session expired or invalidated).
"""

from __future__ import annotations

from collections.abc import Generator

import httpx


class XnatSessionAuth(httpx.Auth):
    requires_response_body = True
    session_id: str | None = None  # class default: not logged in yet

    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.login_url = base_url.rstrip("/") + "/data/JSESSION"
        self._basic = httpx.BasicAuth(user, password)

    def _login(self) -> Generator[httpx.Request, httpx.Response, None]:
        request = httpx.Request("POST", self.login_url)
        next(self._basic.auth_flow(request))  # adds the Authorization header
        response = yield request
        response.raise_for_status()
        self.session_id = response.text.strip() or None

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if self.session_id is None:
            yield from self._login()
        request.headers["Cookie"] = f"JSESSIONID={self.session_id}"
        response = yield request
        if response.status_code == 401:
            yield from self._login()
            request.headers["Cookie"] = f"JSESSIONID={self.session_id}"
            yield request
