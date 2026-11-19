from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scoutoss_common.audit import AuditLogger
from scoutoss_common.auth import JWTValidator
from scoutoss_indexer.app import IndexerDeps, create_app
from scoutoss_indexer.clinical import ClinicalIngestor, MemoryClinicalRepo
from scoutoss_indexer.cursor import MemoryCursor
from scoutoss_indexer.deid.vault import MemoryVault
from scoutoss_indexer.pipeline import Pipeline
from scoutoss_indexer.sources.folder import FolderSource
from scoutoss_indexer.store import IndexStore
from scoutoss_indexer.worker import Worker
from tests.support.fake_opensearch import FakeOpenSearch

ISSUER = "https://auth.test/realms/scoutoss"


@pytest.fixture
def deps(
    fake_os: FakeOpenSearch, rsa_keys: dict[str, Any], salt: bytes, tmp_cohort: Path
) -> IndexerDeps:
    store = IndexStore(fake_os)  # type: ignore[arg-type]
    vault = MemoryVault({"SCOUT_DEMO": -45})
    pipeline = Pipeline(store, salt=salt, vault=vault, default_project="SCOUT_DEMO")
    return IndexerDeps(
        store=store,
        validator=JWTValidator(issuer=ISSUER, audience="scoutoss-indexer", jwks=rsa_keys["jwks"]),
        audit=AuditLogger(fake_os, service="indexer"),
        worker=Worker(pipeline, FolderSource(tmp_cohort, project="SCOUT_DEMO"), poll_seconds=0.05),
        cursor=MemoryCursor(),
        clinical=ClinicalIngestor(
            store, MemoryClinicalRepo(), salt=salt, vault=vault, default_project="SCOUT_DEMO"
        ),
        webhook_token="hook-secret",
        ready_checks=(lambda: True,),
    )


@pytest.fixture
def client(deps: IndexerDeps) -> Iterator[TestClient]:
    with TestClient(create_app(deps)) as c:
        yield c


def _auth(make_token: Callable[..., str], *roles: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(roles=roles, audience='scoutoss-indexer')}"}


def test_health_ready_metrics(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").json()["checks"] == {"opensearch": True, "<lambda>": True}
    body = client.get("/metrics").text
    assert "indexer_documents_indexed_total" in body and "indexer_dlq_total" in body


def test_ready_fails_when_dependency_down(deps: IndexerDeps) -> None:
    def postgres() -> bool:
        raise ConnectionError("down")

    deps.ready_checks = (postgres,)
    deps.worker = None
    with TestClient(create_app(deps)) as c:
        resp = c.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["detail"]["checks"]["postgres"] is False


def test_health_fails_if_worker_dead(deps: IndexerDeps) -> None:
    app = create_app(deps)
    with TestClient(app) as c:
        assert deps.worker is not None
        deps.worker.stop()
        assert c.get("/health").status_code == 503


def test_auth_required_and_roles(client: TestClient, make_token: Callable[..., str]) -> None:
    assert client.get("/schema").status_code == 401
    wrong_aud = {"Authorization": f"Bearer {make_token(audience='scoutoss-copilot')}"}
    assert client.get("/schema").status_code == 401
    assert client.get("/schema", headers=wrong_aud).status_code == 401
    assert (
        client.post("/reindex", json={}, headers=_auth(make_token, "researcher")).status_code == 403
    )


def test_schema_and_stats(
    client: TestClient, make_token: Callable[..., str], deps: IndexerDeps
) -> None:
    import time

    assert deps.worker is not None
    deadline = time.time() + 10
    while deps.worker.state.cycles < 1 and time.time() < deadline:
        time.sleep(0.02)
    schema = client.get("/schema", headers=_auth(make_token, "viewer")).json()
    studies = schema["indices"]["dicom_studies"]
    assert schema["join_field"] == "patient_pseudonym"
    assert studies["doc_count"] == 10
    assert "CT" in studies["examples"]["modalities"]
    assert "patient_age" in studies["fields"]
    stats = client.get("/stats", headers=_auth(make_token, "researcher")).json()
    assert stats["counts"]["dicom_series"] == 12 and stats["worker"]["cycles"] >= 1


def test_reindex_is_audited(
    client: TestClient, make_token: Callable[..., str], fake_os: FakeOpenSearch
) -> None:
    resp = client.post(
        "/reindex", json={"project": "SCOUT_DEMO"}, headers=_auth(make_token, "admin")
    )
    assert resp.status_code == 200 and resp.json()["status"] == "scheduled"
    audit = fake_os.docs("audit")
    assert audit[-1]["action"] == "reindex" and audit[-1]["user"].startswith("u_")


def test_reindex_fails_closed_without_audit(
    client: TestClient, make_token: Callable[..., str], fake_os: FakeOpenSearch
) -> None:
    fake_os.fail_index.add("audit")
    assert client.post("/reindex", json={}, headers=_auth(make_token, "admin")).status_code == 503


def test_webhook(client: TestClient) -> None:
    assert client.post("/webhook/xnat", json={"experiment_id": "E1"}).status_code == 401
    bad = client.post("/webhook/xnat", json={}, headers={"X-Scout-Webhook-Token": "nope"})
    assert bad.status_code == 401
    ok = client.post(
        "/webhook/xnat",
        json={"experiment_id": "E1"},
        headers={"X-Scout-Webhook-Token": "hook-secret"},
    )
    assert ok.status_code == 202


def test_clinical_endpoint(
    client: TestClient, make_token: Callable[..., str], fake_os: FakeOpenSearch
) -> None:
    event = {
        "message_control_id": "MSG1",
        "message_type": "ADT^A01",
        "patient_id": "MRN100001",
        "patient_sex": "M",
        "birth_date": "19500314",
        "event_datetime": "20220105101500",
        "diagnoses": [{"code": "C34.90", "system": "I10", "text": "Malignant neoplasm of lung"}],
    }
    researcher = _auth(make_token, "researcher")
    assert (
        client.post("/clinical/events", json={"events": [event]}, headers=researcher).status_code
        == 403
    )
    resp = client.post(
        "/clinical/events", json={"events": [event]}, headers=_auth(make_token, "service")
    )
    assert resp.status_code == 200 and resp.json()["documents"] == 1
    assert "MRN100001" not in str(fake_os.docs("clinical_events"))
    bad = dict(event, message_type="ADT^A99")
    assert (
        client.post(
            "/clinical/events", json={"events": [bad]}, headers=_auth(make_token, "service")
        ).status_code
        == 422
    )


def test_clinical_disabled(deps: IndexerDeps, make_token: Callable[..., str]) -> None:
    deps.clinical = None
    deps.worker = None
    with TestClient(create_app(deps)) as c:
        resp = c.post(
            "/clinical/events",
            headers=_auth(make_token, "service"),
            json={
                "events": [
                    {"message_control_id": "1", "message_type": "ORM^O01", "patient_id": "X"}
                ]
            },
        )
        assert resp.status_code == 503
