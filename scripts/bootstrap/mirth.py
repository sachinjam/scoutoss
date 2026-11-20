"""Deploy the ScoutOSS HL7 channel to Mirth Connect (idempotent).

* rotates the first-boot admin/admin password to MIRTH_ADMIN_PASSWORD
* imports services/hl7-gateway/channel/scoutoss-hl7-ingest.xml (create or overwrite)
* deploys it and waits until the MLLP listener is STARTED
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import env, fail, log, wait_for

JOB = "mirth"
API = env("MIRTH_URL", "https://mirth:8443") + "/api"
ROOT = Path(env("BOOTSTRAP_ROOT", "/bootstrap"))
CHANNEL = ROOT / "services" / "hl7-gateway" / "channel" / "scoutoss-hl7-ingest.xml"
HEADERS = {
    "X-Requested-With": "OpenAPI",
    "Accept": "application/json, application/xml, text/plain, */*",
}


def client() -> httpx.Client:
    # Mirth's API listens only on the internal network with its own self-signed cert.
    return httpx.Client(base_url=API, headers=HEADERS, verify=False, timeout=60)  # noqa: S501


def login(c: httpx.Client, password: str) -> bool:
    resp = c.post("/users/_login", data={"username": "admin", "password": password})
    return resp.status_code == 200 and "SUCCESS" in resp.text.upper()


def main() -> None:
    c = client()
    wait_for(
        JOB, "Mirth API", lambda: c.get("/server/version").status_code in (200, 401), timeout=900
    )
    wanted = env("MIRTH_ADMIN_PASSWORD")
    if not login(c, wanted):
        if not login(c, "admin"):
            fail(JOB, "cannot log in to Mirth with MIRTH_ADMIN_PASSWORD or the first-boot default")
        me = c.get("/users/current").json()
        user_id = me.get("user", me).get("id")
        resp = c.put(
            f"/users/{user_id}/password", content=wanted, headers={"Content-Type": "text/plain"}
        )
        if resp.status_code >= 300:
            fail(JOB, "password rotation failed", status=resp.status_code)
        log(JOB, "admin password rotated from first-boot default")

    xml = CHANNEL.read_text()
    channel_id = xml.split("<id>", 1)[1].split("</id>", 1)[0]
    exists = c.get(f"/channels/{channel_id}").status_code == 200
    if exists:
        resp = c.put(
            f"/channels/{channel_id}",
            params={"override": "true"},
            content=xml,
            headers={"Content-Type": "application/xml"},
        )
    else:
        resp = c.post("/channels", content=xml, headers={"Content-Type": "application/xml"})
    if resp.status_code >= 300 or resp.text.strip().lower() == "false":
        fail(JOB, "channel import failed", status=resp.status_code, body=resp.text[:500])
    log(JOB, "channel imported", channel=channel_id, updated=exists)

    resp = c.post(f"/channels/{channel_id}/_deploy", params={"returnErrors": "true"})
    if resp.status_code >= 300:
        fail(JOB, "channel deploy failed", status=resp.status_code, body=resp.text[:500])
    deadline = time.time() + 120
    state = None
    while time.time() < deadline:
        status = c.get(f"/channels/{channel_id}/status")
        if status.status_code == 200:
            state = status.json().get("dashboardStatus", {}).get("state")
            if state == "STARTED":
                break
        time.sleep(2)
    if state != "STARTED":
        fail(JOB, "channel did not start", state=state)
    log(JOB, "HL7 channel deployed and listening", port=6661, state=state)


if __name__ == "__main__":
    main()
