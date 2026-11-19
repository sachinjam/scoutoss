"""Copilot end to end with FakeLLM / heuristic planner on real OpenSearch + Postgres,
including the cross-modal query and the audit trail."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opensearchpy import OpenSearch
from psycopg_pool import ConnectionPool

from scoutoss_common.audit import AuditLogger
from scoutoss_common.auth import JWTValidator
from scoutoss_copilot.app import CopilotDeps, create_app
from scoutoss_copilot.clients import static_schema
from scoutoss_copilot.cohorts import ConversationMemory, PostgresCohortRepo
from scoutoss_copilot.executor import Executor
from scoutoss_copilot.heuristic import HeuristicPlanner
from scoutoss_copilot.llm import FakeLLM
from scoutoss_copilot.service import Copilot
from scoutoss_indexer.clinical import ClinicalEvent, ClinicalIngestor, MemoryClinicalRepo
from scoutoss_indexer.deid.pseudonym import pseudonymize
from scoutoss_indexer.deid.vault import MemoryVault
from scoutoss_indexer.pipeline import Pipeline
from scoutoss_indexer.sources.folder import FolderSource
from scoutoss_indexer.store import IndexStore
from scoutoss_indexer.synthetic import LUNG_CANCER_MRNS, PATIENTS, default_cohort, write_cohort

from .conftest import count

ISSUER = "https://auth.test/realms/scoutoss"
SALT = b"integration-salt"


class Schema:
    def get(self) -> dict[str, Any]:
        return static_schema()


@pytest.fixture
def indexed(tmp_path: Path, os_client: OpenSearch) -> OpenSearch:
    store = IndexStore(os_client)
    store.ensure_indices()
    vault = MemoryVault({"SCOUT_DEMO": -40})
    write_cohort(tmp_path / "d", default_cohort(), size=16)
    Pipeline(store, salt=SALT, vault=vault, default_project="SCOUT_DEMO").process(
        FolderSource(tmp_path / "d", project="SCOUT_DEMO").pending()
    )
    events = [
        ClinicalEvent(
            message_control_id=f"A{i}",
            message_type="ADT^A01",
            patient_id=p.mrn,
            patient_sex=p.sex,
            birth_date=p.birth_date.strftime("%Y%m%d"),
            event_datetime="20220110",
            diagnoses=[{"code": "C34.90" if p.mrn in LUNG_CANCER_MRNS else "I10", "text": "dx"}],
        )
        for i, p in enumerate(PATIENTS)
    ]
    ClinicalIngestor(
        store, MemoryClinicalRepo(), salt=SALT, vault=vault, default_project="SCOUT_DEMO"
    ).ingest(events)
    return os_client


@pytest.fixture
def client_factory(
    indexed: OpenSearch, pg: str, rsa_keys: dict[str, Any]
) -> Iterator[Callable[[Any], TestClient]]:
    pool = ConnectionPool(pg, min_size=1, max_size=3, open=True)
    repo = PostgresCohortRepo(pool)
    repo.migrate()
    clients: list[TestClient] = []

    def make(llm: Any) -> TestClient:
        deps = CopilotDeps(
            copilot=Copilot(
                llm=llm,
                executor=Executor(indexed),
                cohorts=repo,
                memory=ConversationMemory(),
                schema=Schema(),
            ),
            validator=JWTValidator(
                issuer=ISSUER, audience="scoutoss-copilot", jwks=rsa_keys["jwks"]
            ),
            audit=AuditLogger(indexed, service="copilot"),
            search=indexed,
        )
        c = TestClient(create_app(deps))
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    pool.close()


def auth(make_token: Callable[..., str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(roles=('researcher',))}"}


def test_query_executes_saves_cohort_and_audits(
    client_factory: Callable[[Any], TestClient], make_token: Callable[..., str], indexed: OpenSearch
) -> None:
    fake = FakeLLM(
        default=json.dumps(
            {
                "steps": [{"index": "dicom_studies", "query": {"term": {"modalities": "CT"}}}],
                "subject": "studies",
            }
        )
    )
    client = client_factory(fake)
    h = auth(make_token)
    resp = client.post("/query", json={"question": "how many CT studies"}, headers=h)
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) >= {"answer_text", "cohort_count", "charts", "generated_query", "cohort_id"}
    assert data["study_count"] == 6 and data["cohort_count"] == 5
    assert data["generated_query"]["steps"][0]["query"] == {"term": {"modalities": "CT"}}
    assert fake.prompts, "FakeLLM was asked"
    cohort_id = data["cohort_id"]
    assert (
        client.post(f"/cohorts/{cohort_id}/save", json={"name": "CT"}, headers=h).json()["saved"]
        is True
    )
    listed = client.get("/cohorts", headers=h).json()["cohorts"]
    assert listed[0]["id"] == cohort_id and listed[0]["member_count"] == 5
    export = client.get(f"/cohorts/{cohort_id}/export", headers=h)
    assert export.status_code == 200 and len(export.text.strip().splitlines()) == 7
    # every action audited, append-only, PHI-free
    assert count(indexed, "audit") == 3
    actions = sorted(
        h["_source"]["action"]
        for h in indexed.search(index="audit", body={"size": 10})["hits"]["hits"]
    )
    assert actions == ["cohort_save", "export", "query"]


def test_cross_modal_query(
    client_factory: Callable[[Any], TestClient], make_token: Callable[..., str]
) -> None:
    client = client_factory(HeuristicPlanner())
    resp = client.post(
        "/query",
        json={"question": "patients under 85 with lung cancer AND CT chest"},
        headers=auth(make_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    # Doe (71, CT chest x2, C34) qualifies; Garcia is 90 (capped) and Müller has no CT *chest*.
    assert data["cohort_count"] == 1
    detail = client.get(f"/cohorts/{data['cohort_id']}", headers=auth(make_token)).json()
    assert detail["members"] == [pseudonymize("MRN100001", SALT)]
    assert data["stats"]["diagnosis"] == {"LUNG_CANCER": 1}
    assert {c["id"] for c in data["charts"]} >= {"age", "sex"}
    assert [s["index"] for s in data["generated_query"]["steps"]] == [
        "dicom_studies",
        "clinical_events",
    ]
    follow = client.post(
        "/query",
        json={"question": "patients with lung cancer", "session_id": "x"},
        headers=auth(make_token),
    ).json()
    assert follow["cohort_count"] == 3


def test_validator_blocks_mutation_against_real_cluster(
    client_factory: Callable[[Any], TestClient], make_token: Callable[..., str], indexed: OpenSearch
) -> None:
    before = count(indexed, "dicom_studies")
    evil = FakeLLM(
        default=json.dumps(
            {
                "steps": [{"index": "dicom_studies", "query": {"match_all": {}}}],
                "note": "POST dicom_studies/_delete_by_query",
            }
        )
    )
    resp = client_factory(evil).post(
        "/query", json={"question": "drop it"}, headers=auth(make_token)
    )
    assert resp.status_code == 422
    assert count(indexed, "dicom_studies") == before
