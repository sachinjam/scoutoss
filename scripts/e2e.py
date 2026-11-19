#!/usr/bin/env python3
"""ScoutOSS end-to-end checks against a running stack (called by scripts/e2e.sh).

Steps (each prints one JSON line; any failure exits non-zero):
  xnat      the pushed study is archived in XNAT under its pseudonym (never the MRN)
  index     its study document is in OpenSearch (within the 60 s budget)
  dashboard the same document is found through OpenSearch Dashboards
  copilot   "how many CT studies" -> count >= 1, cross-modal question answered
  pipeline  ScoutOSS QC launched on the cohort -> a QC assessor appears in XNAT
  audit     query + pipeline launch are in the append-only audit index
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent


def dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


ENV = dotenv()
PORT = ENV.get("HTTPS_PORT", "24443")
PROJECT = ENV.get("DEFAULT_PROJECT", "SCOUT_DEMO")


def step(name: str, ok: bool, **fields: Any) -> None:
    print(json.dumps({"step": name, "ok": ok, **fields}), flush=True)
    if not ok:
        sys.exit(1)


def pseudonym(patient_id: str) -> str:
    salt = ENV["DEID_HMAC_SALT"].encode()
    digest = hmac.new(salt, f"patient:{patient_id.strip().upper()}".encode(), hashlib.sha256).hexdigest()
    return "SP" + digest[:24].upper()


class LocalhostTransport(httpx.HTTPTransport):
    """Resolve *.localhost to 127.0.0.1 (macOS resolvers and some curls do not)."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith(".localhost"):
            request.extensions["sni_hostname"] = request.url.host
            request.headers["host"] = f"{request.url.host}:{request.url.port}"
            request.url = request.url.copy_with(host="127.0.0.1")
        return super().handle_request(request)


def edge() -> httpx.Client:
    ca = str(ROOT / "infra" / "nginx" / "certs" / "ca.crt")
    import ssl

    ctx = ssl.create_default_context(cafile=ca)
    return httpx.Client(transport=LocalhostTransport(verify=ctx), timeout=60)


def compose_exec(service: str, *cmd: str) -> str:
    return subprocess.run(["docker", "compose", "exec", "-T", service, *cmd], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout


def xnat(path: str) -> Any:
    out = compose_exec("xnat-web", "curl", "-s", "-u", f"admin:{ENV['XNAT_ADMIN_PASSWORD']}",
                       f"http://localhost:8080{path}")
    return json.loads(out)


def opensearch(path: str, body: dict[str, Any] | None = None) -> Any:
    cmd = ["curl", "-s", "-H", "Content-Type: application/json", f"http://localhost:9200{path}"]
    if body is not None:
        cmd[1:1] = ["-XPOST", "-d", json.dumps(body)]
    return json.loads(compose_exec("opensearch", *cmd))


def wait(what: str, probe: Any, deadline: float, every: float = 2.0) -> Any:
    while time.time() < deadline:
        try:
            value = probe()
            if value:
                return value
        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, httpx.HTTPError):
            pass
        time.sleep(every)
    return None


def token(client: httpx.Client, user: str = "researcher") -> str:
    resp = client.post(
        f"https://auth.localhost:{PORT}/realms/scoutoss/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": "scoutoss-cli", "username": user,
              "password": ENV["DEMO_USER_PASSWORD"]},
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mrn", required=True, help="PatientID of the pushed study")
    parser.add_argument("--start", type=float, required=True, help="unix time of the C-STORE")
    parser.add_argument("--budget", type=float, default=60.0)
    parser.add_argument("--skip-pipeline", action="store_true")
    parser.add_argument("--pipeline-timeout", type=float, default=300.0)
    args = parser.parse_args()

    pid = pseudonym(args.mrn)
    deadline = args.start + args.budget

    def archived() -> Any:
        rows = xnat("/data/experiments?format=json&columns=ID,label,project,subject_ID,subject_label")["ResultSet"]["Result"]
        return [r for r in rows if r.get("subject_label") == pid]

    sessions = wait("xnat", archived, deadline)
    step("xnat", bool(sessions), seconds=round(time.time() - args.start, 1),
         experiment=sessions[0]["ID"] if sessions else None)
    subjects = xnat(f"/data/projects/{PROJECT}/subjects?format=json&columns=label")["ResultSet"]["Result"]
    step("xnat-deid", all(s["label"] != args.mrn for s in subjects), subjects=len(subjects))

    def indexed() -> Any:
        hits = opensearch("/dicom_studies/_search", {"query": {"term": {"patient_pseudonym": pid}}})["hits"]["hits"]
        return hits

    hits = wait("index", indexed, deadline)
    step("index", bool(hits), seconds=round(time.time() - args.start, 1), within_budget=time.time() <= deadline)
    raw = json.dumps(hits)
    step("index-deid", args.mrn not in raw and "Tester" not in raw)

    dash = compose_exec(
        "opensearch-dashboards", "curl", "-s", "-XPOST", "-H", "osd-xsrf: true", "-H", "Content-Type: application/json",
        "http://localhost:5601/dashboards/api/console/proxy?path=dicom_studies%2F_search&method=POST",
        "-d", json.dumps({"query": {"term": {"patient_pseudonym": pid}}}),
    )
    step("dashboard", json.loads(dash)["hits"]["total"]["value"] >= 1)

    client = edge()
    headers = {"Authorization": f"Bearer {token(client)}"}
    api = f"https://app.localhost:{PORT}/api/copilot"
    resp = client.post(f"{api}/query", json={"question": "how many CT studies", "session_id": "e2e"}, headers=headers)
    data = resp.json()
    step("copilot", resp.status_code == 200 and data.get("study_count", 0) >= 1,
         status=resp.status_code, studies=data.get("study_count"), patients=data.get("cohort_count"))
    cross = client.post(f"{api}/query", json={"question": "patients under 85 with lung cancer AND CT chest"},
                        headers=headers).json()
    step("copilot-cross-modal", "cohort_count" in cross and len(cross["generated_query"]["steps"]) == 2,
         patients=cross.get("cohort_count"))

    if not args.skip_pipeline:
        target = client.post(f"{api}/query", json={"question": "CT chest studies in 2023", "session_id": "e2e-p"},
                             headers=headers).json()
        cohort = target["cohort_id"]
        launch = client.post(f"{api}/cohorts/{cohort}/pipeline", json={"pipeline": "scoutoss-qc-session"},
                             headers=headers)
        launched = launch.json().get("launched", []) if launch.status_code == 200 else []
        step("pipeline-launch", launch.status_code == 200 and bool(launched), status=launch.status_code,
             launched=len(launched), body=None if launch.status_code == 200 else launch.text[:200])
        experiment = launched[0]["experiment_id"]

        def assessor() -> Any:
            rows = xnat(f"/data/experiments/{experiment}/assessors?format=json")["ResultSet"]["Result"]
            return [r for r in rows if "qcAssessmentData" in r.get("xsiType", "")]

        found = wait("assessor", assessor, time.time() + args.pipeline_timeout, every=5)
        step("pipeline-assessor", bool(found), experiment=experiment,
             assessor=found[0].get("label") if found else None)

    def audited() -> Any:
        resp = opensearch("/audit/_search", {"size": 0, "aggs": {"a": {"terms": {"field": "action"}}}})
        actions = {b["key"] for b in resp["aggregations"]["a"]["buckets"]}
        wanted = {"query"} | (set() if args.skip_pipeline else {"pipeline_launch"})
        return actions if wanted <= actions else None

    actions = wait("audit", audited, time.time() + 20)
    step("audit", bool(actions), actions=sorted(actions or []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
