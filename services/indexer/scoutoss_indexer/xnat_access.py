"""Project access sync: Keycloak groups -> XNAT project roles.

Users reach XNAT (and the OHIF viewer inside it) through Keycloak single sign-on; XNAT's OpenID
plugin creates their account on first sign-in but grants no project access. Tenancy in ScoutOSS
is defined once, as Keycloak groups ``/projects/<XNAT project ID>`` (the same groups the copilot
filters by). This job mirrors that membership into XNAT as the read-only ``collaborator`` role:

* a group member who has an XNAT account and no role in the project is granted ``collaborator``;
* a ``collaborator`` who is a Keycloak user but no longer in the group is removed.

It never touches accounts Keycloak does not know (``admin``, the XNAT service accounts) and never
changes ``owner``/``member`` roles, so manual grants made in XNAT are left alone. It runs as its own
service because it needs XNAT site-admin and Keycloak user-lookup credentials that the indexer
should not hold.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

ROLE = "collaborator"
PAGE = 200


class KeycloakDirectory:
    """Reads realm users and ``/projects/*`` group membership with a service account.

    The client needs the realm-management roles ``view-users``, ``query-users`` and
    ``query-groups`` and the ``kc-admin-api`` client scope: recent Keycloak versions reject a
    token on the admin API unless that scope put the admin audience into it.
    """

    def __init__(
        self,
        base_url: str,
        realm: str,
        client_id: str,
        client_secret: str,
        *,
        groups_root: str = "projects",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret
        self.groups_root = groups_root
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=15, transport=transport)
        self._token: str | None = None
        self._expires = 0.0

    def _auth(self) -> dict[str, str]:
        if not self._token or time.time() >= self._expires - 30:
            resp = self.http.post(
                f"/realms/{self.realm}/protocol/openid-connect/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = str(data["access_token"])
            self._expires = time.time() + float(data.get("expires_in", 60))
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Admin API GET. A 401 drops the cached token and retries once with a fresh one, so a
        token minted before a role or scope change does not wedge the loop until it expires."""
        url = f"/admin/realms/{self.realm}{path}"
        resp = self.http.get(url, params=params, headers=self._auth())
        if resp.status_code == 401:
            self._token = None
            resp = self.http.get(url, params=params, headers=self._auth())
        return resp

    def _pages(self, path: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        first = 0
        while True:
            resp = self._get(path, {"first": first, "max": PAGE, "briefRepresentation": "true"})
            resp.raise_for_status()
            page: list[dict[str, Any]] = resp.json()
            rows.extend(page)
            if len(page) < PAGE:
                return rows
            first += PAGE

    def usernames(self) -> set[str]:
        return {str(u["username"]).lower() for u in self._pages("/users") if u.get("username")}

    def project_members(self) -> dict[str, set[str]]:
        """``{XNAT project ID: usernames}`` from the children of the ``/projects`` group."""
        resp = self._get(f"/group-by-path/{self.groups_root}")
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        members: dict[str, set[str]] = {}
        for group in self._pages(f"/groups/{resp.json()['id']}/children"):
            users = self._pages(f"/groups/{group['id']}/members")
            members[str(group["name"])] = {
                str(u["username"]).lower() for u in users if u.get("enabled", True)
            }
        return members


class XnatAccess:
    """The few XNAT admin calls the sync needs."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=(user, password),
            timeout=30,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def _rows(self, path: str) -> list[dict[str, Any]]:
        resp = self.http.get(path, params={"format": "json"})
        resp.raise_for_status()
        rows: list[dict[str, Any]] = resp.json().get("ResultSet", {}).get("Result", [])
        return rows

    def usernames(self) -> set[str]:
        resp = self.http.get("/xapi/users")
        resp.raise_for_status()
        return {str(u).lower() for u in resp.json()}

    def projects(self) -> list[str]:
        return [str(r["ID"]) for r in self._rows("/data/projects") if r.get("ID")]

    def roles(self, project: str) -> dict[str, str]:
        """``{username: role}`` for everyone with a role in the project."""
        out: dict[str, str] = {}
        for row in self._rows(f"/data/projects/{project}/users"):
            login = str(row.get("login", "")).lower()
            role = str(row.get("displayname") or row.get("GROUP_ID") or "").lower()
            if login:
                out[login] = "collaborator" if "collaborator" in role else role
        return out

    def grant(self, project: str, username: str) -> None:
        self.http.put(f"/data/projects/{project}/users/{ROLE}/{username}").raise_for_status()

    def revoke(self, project: str, username: str) -> None:
        self.http.delete(f"/data/projects/{project}/users/{ROLE}/{username}").raise_for_status()


@dataclass
class SyncResult:
    granted: list[tuple[str, str]] = field(default_factory=list)
    revoked: list[tuple[str, str]] = field(default_factory=list)


def sync_once(directory: KeycloakDirectory, xnat: XnatAccess) -> SyncResult:
    result = SyncResult()
    members = directory.project_members()
    known = directory.usernames()
    accounts = xnat.usernames()
    for project in xnat.projects():
        wanted = members.get(project, set()) & accounts
        roles = xnat.roles(project)
        for username in sorted(wanted - set(roles)):
            xnat.grant(project, username)
            result.granted.append((project, username))
        stale = {u for u, role in roles.items() if role == ROLE and u in known and u not in wanted}
        for username in sorted(stale):
            xnat.revoke(project, username)
            result.revoked.append((project, username))
    if result.granted or result.revoked:
        # usernames are Keycloak logins of staff, not patient data; counts only, to be safe
        log.info("project access synced", granted=len(result.granted), revoked=len(result.revoked))
    return result


def run_forever(
    directory: KeycloakDirectory,
    xnat: XnatAccess,
    *,
    interval: float,
    heartbeat: Path | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Sync every ``interval`` seconds. A failed cycle is logged and retried; it never exits."""
    stop = stop or threading.Event()
    log.info("xnat access sync started", interval_seconds=interval)
    while not stop.is_set():
        try:
            sync_once(directory, xnat)
            if heartbeat is not None:
                heartbeat.touch()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("xnat access sync failed", error=type(exc).__name__, detail=str(exc)[:200])
        stop.wait(interval)


def main() -> int:  # pragma: no cover - process entry point
    from scoutoss_common.logging import configure_logging

    configure_logging("xnat-access-sync")
    directory = KeycloakDirectory(
        os.environ["KEYCLOAK_URL"],
        os.environ.get("KEYCLOAK_REALM", "scoutoss"),
        os.environ.get("KC_XNAT_SYNC_CLIENT_ID", "scoutoss-xnat-sync"),
        os.environ["KC_XNAT_SYNC_CLIENT_SECRET"],
    )
    xnat = XnatAccess(
        os.environ["XNAT_URL"],
        os.environ.get("XNAT_ADMIN_USER", "admin"),
        os.environ["XNAT_ADMIN_PASSWORD"],
    )
    run_forever(
        directory,
        xnat,
        interval=float(os.environ.get("ACCESS_SYNC_SECONDS", "5")),
        heartbeat=Path(os.environ.get("ACCESS_SYNC_HEARTBEAT", "/tmp/xnat-access-sync.ok")),  # noqa: S108
    )
    return 0
