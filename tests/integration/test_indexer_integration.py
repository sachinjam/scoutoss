"""Indexer against real Postgres + OpenSearch: 10 synthetic DICOM files + 2 corrupt."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from opensearchpy import OpenSearch
from psycopg_pool import ConnectionPool

from scoutoss_indexer.clinical import ClinicalEvent, ClinicalIngestor, PostgresClinicalRepo
from scoutoss_indexer.cursor import PostgresCursor
from scoutoss_indexer.deid.pseudonym import pseudonymize
from scoutoss_indexer.deid.vault import PostgresVault
from scoutoss_indexer.pipeline import Pipeline
from scoutoss_indexer.sources.folder import FolderSource
from scoutoss_indexer.store import IndexStore
from scoutoss_indexer.synthetic import default_cohort, write_cohort, write_corrupt

from .conftest import count

SALT = b"integration-salt"
KEY = bytes(range(32))


def ten_files(root: Path) -> Path:
    studies = default_cohort()
    for s in studies:
        s.series = s.series[:1]
        s.series[0].slices = 1
    write_cohort(root / "dicom", studies, size=16)
    write_corrupt(root / "dicom" / "bad")
    return root / "dicom"


@pytest.fixture
def setup(
    tmp_path: Path, os_client: OpenSearch, pg: str
) -> tuple[Pipeline, IndexStore, PostgresVault, Path]:
    store = IndexStore(os_client, xnat_public_url="https://xnat.test")
    store.ensure_indices()
    vault = PostgresVault(pg, KEY)
    vault.migrate()
    return (
        Pipeline(store, salt=SALT, vault=vault, default_project="SCOUT_DEMO"),
        store,
        vault,
        ten_files(tmp_path),
    )


def test_folder_index_counts_fields_and_dlq(
    setup: tuple[Pipeline, IndexStore, PostgresVault, Path], os_client: OpenSearch, pg: str
) -> None:
    pipeline, _store, vault, folder = setup
    assert len(list(folder.rglob("*.dcm"))) == 12
    result = pipeline.process(FolderSource(folder, project="SCOUT_DEMO").pending())
    assert result.indexed == 10 and result.dead_lettered == 2
    assert count(os_client, "dicom_series") == 10
    assert count(os_client, "dicom_studies") == 10
    assert count(os_client, "dicom_patients") == 7
    assert count(os_client, "indexer_dlq") == 2
    # field values (keyword normalizer makes term queries case-insensitive)
    assert count(os_client, "dicom_studies", {"term": {"modalities": "ct"}}) == 6
    assert count(os_client, "dicom_studies", {"range": {"patient_age": {"gte": 90}}}) == 1
    doe = pseudonymize("MRN100001", SALT)
    assert count(os_client, "dicom_patients", {"term": {"patient_pseudonym": doe}}) == 1
    patient = os_client.get(index="dicom_patients", id=doe)["_source"]
    assert patient["study_count"] == 2 and patient["patient_sex"] == "M"
    raw = str(os_client.search(index="dicom_*", body={"size": 100})["hits"]["hits"])
    for phi in ("MRN1000", "Doe^John", "Springfield", "742 Evergreen", "House^Gregory"):
        assert phi not in raw
    # the only re-identification path: the encrypted vault (ciphertext at rest)
    assert vault.reveal(doe) == "MRN100001"
    with psycopg.connect(pg) as conn:
        blob = conn.execute(
            "SELECT patient_id_enc FROM deid.subject_map WHERE pseudonym = %s", (doe,)
        ).fetchone()
        assert blob is not None and b"MRN100001" not in bytes(blob[0])
        assert conn.execute("SELECT count(*) FROM deid.project_offset").fetchone() == (1,)


def test_reindex_is_idempotent(
    setup: tuple[Pipeline, IndexStore, PostgresVault, Path], os_client: OpenSearch
) -> None:
    pipeline, _, _, folder = setup
    source = FolderSource(folder, project="SCOUT_DEMO")
    pipeline.process(source.pending())
    first = {
        i: count(os_client, i)
        for i in ("dicom_series", "dicom_studies", "dicom_patients", "indexer_dlq")
    }
    pipeline.process(source.pending(rescan=True))
    pipeline.process(source.pending(rescan=True))
    assert {i: count(os_client, i) for i in first} == first
    dlq = os_client.search(index="indexer_dlq", body={"size": 10})["hits"]["hits"]
    assert all(h["_source"]["attempts"] == 3 for h in dlq)


def test_vault_offset_is_stable_across_instances(pg: str) -> None:
    a, b = PostgresVault(pg, KEY), PostgresVault(pg, KEY)
    a.migrate()
    assert a.project_offset("P1") == b.project_offset("P1")
    assert -730 <= a.project_offset("P1") <= -30
    assert b.reveal("SPNOPE") is None


def test_cursor_and_clinical_repo(pg: str, os_client: OpenSearch) -> None:
    pool = ConnectionPool(pg, min_size=1, max_size=2, open=True)
    try:
        cursor = PostgresCursor(pool)
        cursor.migrate()
        cursor.mark("E1", project="P", change_token="t1", status="error", error="Timeout")
        cursor.mark("E1", project="P", change_token="t1", status="error", error="Timeout")
        assert cursor.get_all()["E1"].attempts == 2
        cursor.mark("E1", project="P", change_token="t2", status="indexed", series_indexed=3)
        assert cursor.get_all()["E1"].attempts == 0 and cursor.get_all()["E1"].status == "indexed"
        cursor.mark("E2", project="Q", change_token="t", status="indexed")
        cursor.touch_poll("xnat", ok=True)
        cursor.touch_poll("xnat", ok=False)
        assert cursor.reset(project="P") == 1 and cursor.reset(experiment_ids=["E2"]) == 1
        assert cursor.reset() == 0

        store = IndexStore(os_client)
        store.ensure_indices()
        repo = PostgresClinicalRepo(pool)
        repo.migrate()
        vault = PostgresVault(pg, KEY)
        vault.migrate()
        ingest = ClinicalIngestor(store, repo, salt=SALT, vault=vault, default_project="SCOUT_DEMO")
        event = ClinicalEvent(
            message_control_id="M1",
            message_type="ADT^A01",
            patient_id="MRN100001",
            patient_sex="M",
            birth_date="19500314",
            event_datetime="20220105",
            diagnoses=[{"code": "C34.90", "text": "Malignant neoplasm of lung"}],
        )
        ingest.ingest([event, event])
        assert count(os_client, "clinical_events") == 1
        with pool.connection() as conn:
            assert conn.execute("SELECT count(*) FROM clinical.events").fetchone() == (1,)
            assert conn.execute("SELECT count(*) FROM clinical.patients").fetchone() == (1,)
    finally:
        pool.close()
