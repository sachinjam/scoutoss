"""OpenSearch first-boot setup: ISM retention policies and the logs-* template.

* ``logs-retention``   delete Filebeat ``logs-*`` indices after 30 days
* ``audit-retention``  keep the audit trail 6 years (HIPAA 45 CFR 164.316(b)(2)), then delete;
                       the audit index is also made read-only for updates/deletes via
                       ``index.blocks`` in production (see docs/compliance.md)
Then imports the OpenSearch Dashboards saved objects (dashboards/*.ndjson).
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import env, fail, log, wait_for

JOB = "opensearch"
OS_URL = env("OPENSEARCH_URL", "http://opensearch:9200")
OSD_URL = env("DASHBOARDS_URL", "http://opensearch-dashboards:5601")
ROOT = Path(env("BOOTSTRAP_ROOT", "/bootstrap"))


def policy(days: str, pattern: str, description: str) -> dict[str, object]:
    return {
        "policy": {
            "description": description,
            "default_state": "hot",
            "states": [
                {
                    "name": "hot",
                    "actions": [],
                    "transitions": [
                        {"state_name": "delete", "conditions": {"min_index_age": days}}
                    ],
                },
                {"name": "delete", "actions": [{"delete": {}}], "transitions": []},
            ],
            "ism_template": [{"index_patterns": [pattern], "priority": 100}],
        }
    }


def put_policy(client: httpx.Client, name: str, body: dict[str, object]) -> None:
    existing = client.get(f"/_plugins/_ism/policies/{name}")
    if existing.status_code == 200:
        data = existing.json()
        resp = client.put(
            f"/_plugins/_ism/policies/{name}",
            params={"if_seq_no": data["_seq_no"], "if_primary_term": data["_primary_term"]},
            json=body,
        )
    else:
        resp = client.put(f"/_plugins/_ism/policies/{name}", json=body)
    if resp.status_code >= 300:
        fail(JOB, "ISM policy failed", policy=name, status=resp.status_code, body=resp.text[:300])
    log(JOB, "ISM policy applied", policy=name)


def main() -> None:
    client = httpx.Client(base_url=OS_URL, timeout=60)
    wait_for(JOB, "OpenSearch", lambda: client.get("/_cluster/health").status_code == 200)
    put_policy(client, "logs-retention", policy("30d", "logs-*", "Filebeat logs: 30-day retention"))
    put_policy(
        client, "audit-retention", policy("2190d", "audit*", "Audit trail: 6-year retention")
    )
    template = {
        "index_patterns": ["logs-*"],
        "template": {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {
                "properties": {
                    "ts": {"type": "date"},
                    "level": {"type": "keyword"},
                    "service": {"type": "keyword"},
                    "env": {"type": "keyword"},
                    "trace_id": {"type": "keyword"},
                    "msg": {"type": "text"},
                    "user_id": {"type": "keyword"},
                    "endpoint": {"type": "keyword"},
                    "latency_ms": {"type": "float"},
                    "status": {"type": "integer"},
                }
            },
        },
        "priority": 100,
    }
    resp = client.put("/_index_template/scoutoss-logs", json=template)
    if resp.status_code >= 300:
        fail(JOB, "logs template failed", status=resp.status_code, body=resp.text[:300])
    log(JOB, "logs-* index template applied")

    dash = httpx.Client(base_url=OSD_URL, timeout=60, headers={"osd-xsrf": "true"})
    wait_for(
        JOB,
        "OpenSearch Dashboards",
        lambda: dash.get("/api/status").status_code == 200,
        timeout=600,
    )
    for path in sorted((ROOT / "dashboards").glob("*.ndjson")):
        resp = dash.post(
            "/api/saved_objects/_import",
            params={"overwrite": "true"},
            files={"file": (path.name, path.read_bytes(), "application/ndjson")},
        )
        body = (
            resp.json()
            if resp.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        if resp.status_code >= 300 or not body.get("success", False):
            fail(
                JOB,
                "saved objects import failed",
                file=path.name,
                status=resp.status_code,
                errors=str(body.get("errors", resp.text))[:500],
            )
        log(JOB, "saved objects imported", file=path.name, count=body.get("successCount"))
    dash.post(
        "/api/opensearch-dashboards/settings",
        json={"changes": {"defaultIndex": "scoutoss-studies", "theme:darkMode": False}},
    )
    log(JOB, "OpenSearch bootstrap complete")


if __name__ == "__main__":
    main()
