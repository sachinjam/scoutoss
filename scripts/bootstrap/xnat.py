"""Configure XNAT for ScoutOSS (idempotent; safe to re-run).

* admin password from XNAT_ADMIN_PASSWORD (first boot ships admin/admin)
* site config: URL, login required, no self-registration, fast session archiving
* project SCOUT_DEMO with auto-archive and the project DicomEdit script
* DICOM SCP receiver (AE XNAT, port 8104) accepting only the de-id gateway (AE SCOUTGW)
* service accounts: scout_indexer (read-only collaborator), scout_pipeline (member)
* Container Service: job engine + ScoutOSS QC and TotalSegmentator commands
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import env, fail, log, wait_for

JOB = "xnat"
XNAT = env("XNAT_URL", "http://xnat-web:8080")
PROJECT = env("DEFAULT_PROJECT", "SCOUT_DEMO")
ROOT = Path(env("BOOTSTRAP_ROOT", "/bootstrap"))


class Xnat:
    def __init__(self, user: str, password: str) -> None:
        self.http = httpx.Client(
            base_url=XNAT,
            auth=(user, password),
            timeout=120,
            headers={"Accept": "application/json, text/plain, */*"},
        )

    def call(
        self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201, 204), **kw: Any
    ) -> httpx.Response:
        resp = self.http.request(method, path, **kw)
        if resp.status_code not in ok:
            fail(
                JOB,
                "XNAT call failed",
                method=method,
                path=path,
                status=resp.status_code,
                body=resp.text[:300],
            )
        return resp

    def json(self, method: str, path: str, **kw: Any) -> Any:
        resp = self.call(method, path, **kw)
        return resp.json() if resp.content else None


def admin_session(attempts: int = 8, grace: float = 60, every: float = 45) -> Xnat:
    """Log in as admin without tripping XNAT's failed-login lockout.

    XNAT answers HTTP before its auth records exist on first boot, and every wrong password
    counts towards the lockout. So: wait a grace period, try the first-boot default first
    (only valid on a fresh install), then the configured password, and pace the attempts so
    a whole run stays well under the lockout threshold.
    """
    import time

    wanted = env("XNAT_ADMIN_PASSWORD")
    time.sleep(grace)
    for attempt in range(1, attempts + 1):
        for password in ("admin", wanted):
            try:
                ok = (
                    httpx.get(
                        f"{XNAT}/xapi/siteConfig/buildInfo", auth=("admin", password), timeout=30
                    ).status_code
                    == 200
                )
            except httpx.HTTPError:
                ok = False
            if not ok:
                continue
            if password == "admin":  # noqa: S105 - XNAT first-boot default, rotated below
                Xnat("admin", "admin").call("PUT", "/xapi/users/admin", json={"password": wanted})
                log(JOB, "admin password rotated from first-boot default")
            return Xnat("admin", wanted)
        log(JOB, "XNAT not ready for admin login yet", attempt=attempt, of=attempts)
        time.sleep(every)
    fail(JOB, "cannot log in as admin with XNAT_ADMIN_PASSWORD or the first-boot default")
    raise SystemExit(1)


def configure_site(x: Xnat) -> None:
    settings = {
        "siteId": "ScoutOSS",
        "siteUrl": env("XNAT_PUBLIC_URL"),
        "adminEmail": "admin@scoutoss.local",
        "initialized": True,
        "requireLogin": True,
        "userRegistration": False,
        "enableCsrfToken": True,
        "securityAllowNonPrivateProjects": False,
        "restrictUserListAccessToAdmins": True,
        # archive sessions quickly so a C-STORE is searchable within 60 s
        "sessionXmlRebuilderInterval": 1,
        "sessionXmlRebuilderRepeat": 10000,
        # The stock site-wide script rewrites PatientID/PatientName to XNAT labels, which
        # would break the gateway's pseudonyms. De-id is done by the gateway + project script.
        "enableSitewideAnonymizationScript": False,
    }
    x.call("POST", "/xapi/siteConfig", json=settings)
    log(JOB, "site configured", keys=len(settings))


def ensure_project(x: Xnat) -> None:
    if x.http.get(f"/data/projects/{PROJECT}", params={"format": "json"}).status_code != 200:
        xml = (
            '<xnat:projectData xmlns:xnat="http://nrg.wustl.edu/xnat" '
            f'ID="{PROJECT}" secondary_ID="{PROJECT}"><xnat:name>ScoutOSS demo cohort</xnat:name>'
            "<xnat:description>Synthetic, de-identified demo data</xnat:description>"
            "</xnat:projectData>"
        )
        x.call(
            "POST",
            "/data/projects",
            content=xml,
            headers={"Content-Type": "text/xml"},
            ok=(200, 201, 409),
        )
        log(JOB, "project created", project=PROJECT)
    access = x.call("GET", f"/data/projects/{PROJECT}/accessibility").text.strip()
    if access != "private":  # new projects are private; non-private ones are disallowed site-wide
        fail(JOB, "project is not private", project=PROJECT, accessibility=access)
    x.call("PUT", f"/data/projects/{PROJECT}/prearchive_code/4", ok=(200, 201, 204))
    script = (ROOT / "xnat" / "anon" / "scoutoss.das").read_text()
    x.call(
        "PUT",
        f"/data/projects/{PROJECT}/config/anon/projects/{PROJECT}",
        params={"inbody": "true"},
        content=script,
        headers={"Content-Type": "text/plain"},
    )
    x.call(
        "PUT",
        f"/data/projects/{PROJECT}/config/anon/projects/{PROJECT}",
        params={"status": "enabled"},
    )
    log(JOB, "project archiving and DicomEdit anonymization configured", project=PROJECT)


def ensure_receiver(x: Xnat) -> None:
    receivers = x.json("GET", "/xapi/dicomscp") or []
    wanted = {
        "aeTitle": "XNAT",
        "port": 8104,
        "enabled": True,
        "customProcessing": False,
        "directArchive": False,
        "anonymizationEnabled": True,
        "identifier": "dicomObjectIdentifier",
        "whitelistEnabled": True,
        "whitelist": [env("GATEWAY_AE_TITLE", "SCOUTGW")],
    }
    existing = next(
        (r for r in receivers if r.get("aeTitle") == "XNAT" and r.get("port") == 8104), None
    )
    if existing:
        x.call("PUT", f"/xapi/dicomscp/{existing['id']}", json={**existing, **wanted})
    else:
        x.call("POST", "/xapi/dicomscp", json=wanted)
    log(JOB, "DICOM SCP receiver XNAT:8104 accepts only the de-id gateway")


def ensure_user(x: Xnat, username: str, password: str, role: str) -> None:
    users = x.json("GET", "/xapi/users") or []
    profile = {
        "username": username,
        "password": password,
        "firstName": "ScoutOSS",
        "lastName": username,
        "email": f"{username}@scoutoss.local",
        "enabled": True,
        "verified": True,
    }
    if username in users:
        x.call("PUT", f"/xapi/users/{username}", json=profile)
    else:
        x.call("POST", "/xapi/users", json=profile)
    x.call("PUT", f"/data/projects/{PROJECT}/users/{role}/{username}", ok=(200, 201, 204, 409))
    log(JOB, "service account ready", user=username, project_role=role)


def ensure_container_service(x: Xnat) -> list[str]:
    server = {
        "name": "scoutoss-job-engine",
        "host": env("JOB_ENGINE_URL", "tcp://job-engine:2375"),
        "backend": "docker",
        "swarm-mode": False,
        "pull-images-on-xnat-init": False,
        "auto-cleanup": True,
        # job containers run as XNAT's uid so they can write the build (output) directory
        "container-user": "1000:1000",
        "status-email-enabled": False,
    }
    x.call("POST", "/xapi/docker/server", json=server)
    ping = x.http.get("/xapi/docker/server/ping")
    log(JOB, "container service job engine registered", ping=ping.text.strip()[:20])
    wrappers: list[str] = []
    existing = {c["name"]: c for c in x.json("GET", "/xapi/commands") or []}
    for path in sorted((ROOT / "xnat" / "commands").glob("*.json")):
        command = json.loads(path.read_text())
        if command["name"] in existing:
            cmd_id = existing[command["name"]]["id"]
            command["id"] = cmd_id
            x.call("POST", f"/xapi/commands/{cmd_id}", json=command, ok=(200, 201, 204, 409))
        else:
            x.call("POST", "/xapi/commands", json=command, ok=(200, 201, 409))
        current = {c["name"]: c for c in x.json("GET", "/xapi/commands") or []}[command["name"]]
        for wrapper in current.get("xnat", []):
            x.call("PUT", f"/xapi/wrappers/{wrapper['id']}/enabled")
            x.call("PUT", f"/xapi/projects/{PROJECT}/wrappers/{wrapper['id']}/enabled")
            wrappers.append(wrapper["name"])
    log(JOB, "pipelines registered and enabled", wrappers=wrappers)
    return wrappers


def main() -> None:
    wait_for(
        JOB,
        "XNAT",
        lambda: httpx.get(f"{XNAT}/", timeout=10).status_code in (200, 302, 401),
        timeout=1800,
        every=10,
    )
    x = admin_session()
    configure_site(x)
    ensure_project(x)
    ensure_receiver(x)
    ensure_user(x, "scout_indexer", env("XNAT_INDEXER_PASSWORD"), "Collaborators")
    ensure_user(x, "scout_pipeline", env("XNAT_PIPELINE_PASSWORD"), "Members")
    ensure_user(x, "scout_archiver", env("XNAT_ARCHIVER_PASSWORD"), "Members")
    if env("XNAT_SKIP_CONTAINER_SERVICE", "false") != "true":
        ensure_container_service(x)
    log(JOB, "XNAT bootstrap complete", project=PROJECT)


if __name__ == "__main__":
    main()
