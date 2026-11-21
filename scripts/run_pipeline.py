#!/usr/bin/env python3
"""Run an AI pipeline on a whole cohort through the copilot, then verify every result.

    make qc                                   # QC on every study the demo user can see
    python scripts/run_pipeline.py --question "CT chest" --pipeline scoutoss-qc-session

This is the scripted form of the UI's "Run Pipeline on Cohort": ask the copilot a question
(which defines the cohort), launch the pipeline on it, then wait until each launched session has
its assessor in XNAT. Prints one JSON line per step and exits non-zero if any session is left
without a result, with XNAT's container records to explain why.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e import ENV, PORT, edge, pipeline_diagnostics, step, xnat

API = f"https://app.localhost:{PORT}/api/copilot"
ASSESSOR_TYPE = {"scoutoss-qc-session": "qcAssessmentData"}


def demo_token(client: Any, user: str, password: str) -> str:
    resp = client.post(
        f"https://auth.localhost:{PORT}/realms/scoutoss/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "scoutoss-cli",
            "username": user,
            "password": password,
        },
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


def assessors(xsi_fragment: str) -> dict[str, str]:
    """``{assessor id: session id}`` for every assessor of the pipeline's type."""
    rows = xnat(f"/data/experiments?format=json&xsiType=xnat:{xsi_fragment}&columns=session_ID")
    return {
        str(row.get(f"xnat:{xsi_fragment.lower()}/id") or row.get("ID")): str(row.get("session_ID"))
        for row in rows["ResultSet"]["Result"]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--question", default="how many studies", help="defines the cohort")
    parser.add_argument("--pipeline", default="scoutoss-qc-session")
    parser.add_argument("--user", default="demo")
    parser.add_argument("--password", default=None, help="default: demo, or DEMO_USER_PASSWORD")
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()
    password = args.password or ("demo" if args.user == "demo" else ENV["DEMO_USER_PASSWORD"])

    client = edge()
    client.timeout = 300
    auth = {"Authorization": f"Bearer {demo_token(client, args.user, password)}"}
    answer = client.post(f"{API}/query", json={"question": args.question}, headers=auth)
    body = answer.json() if answer.status_code == 200 else {}
    step(
        "cohort",
        answer.status_code == 200 and bool(body.get("cohort_id")),
        question=args.question,
        patients=body.get("cohort_count"),
        studies=body.get("study_count"),
        status=answer.status_code,
    )

    kind = ASSESSOR_TYPE.get(args.pipeline)
    before = set(assessors(kind)) if kind else set()  # only results made by this run count
    started = time.time()
    launch = client.post(
        f"{API}/cohorts/{body['cohort_id']}/pipeline",
        json={"pipeline": args.pipeline},
        headers=auth,
    )
    result = launch.json() if launch.status_code == 200 else {}
    launched = [item["experiment_id"] for item in result.get("launched", [])]
    step(
        "launch",
        launch.status_code == 200 and bool(launched) and not result.get("errors"),
        pipeline=args.pipeline,
        launched=len(launched),
        errors=result.get("errors", [])[:5],
        status=launch.status_code,
        body=None if launch.status_code == 200 else launch.text[:300],
    )

    if kind is None:
        step("results", True, note="no result check known for this pipeline")
        return 0
    done: set[str] = set()
    deadline = started + args.timeout
    while time.time() < deadline:
        done = {
            session
            for assessor, session in assessors(kind).items()
            if assessor not in before and session in launched
        }
        if len(done) == len(launched):
            break
        time.sleep(5)
    missing = sorted(set(launched) - done)
    step(
        "results",
        not missing,
        sessions_with_result=len(done),
        launched=len(launched),
        seconds=round(time.time() - started, 1),
        missing=missing[:10],
        **({} if not missing else pipeline_diagnostics()),
    )
    print("PIPELINE RUN PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
