"""Idempotent bulk indexing, rollups and dead-letter routing against a fake OpenSearch."""

from __future__ import annotations

from pathlib import Path

import pytest

from scoutoss_common.catalog import DLQ_INDEX, PATIENTS_INDEX, SERIES_INDEX, STUDIES_INDEX
from scoutoss_indexer.deid.vault import MemoryVault
from scoutoss_indexer.pipeline import Pipeline, WorkItem
from scoutoss_indexer.sources.folder import FolderSource
from scoutoss_indexer.store import IndexStore, build_patient_doc, build_study_doc
from scoutoss_indexer.synthetic import write_corrupt
from tests.support.fake_opensearch import FakeOpenSearch


@pytest.fixture
def store(fake_os: FakeOpenSearch) -> IndexStore:
    s = IndexStore(fake_os, xnat_public_url="https://xnat.example")  # type: ignore[arg-type]
    s.ensure_indices()
    return s


def _pipeline(store: IndexStore, salt: bytes) -> Pipeline:
    return Pipeline(store, salt=salt, vault=MemoryVault({"COHORT": -60}), default_project="COHORT")


def test_ensure_indices_idempotent(store: IndexStore, fake_os: FakeOpenSearch) -> None:
    import copy

    fake_os.mappings["clinical_events"] = copy.deepcopy(fake_os.mappings["clinical_events"])
    fake_os.mappings["clinical_events"]["mappings"]["properties"].pop("project")
    assert store.ensure_indices() == []
    assert (
        "project" in fake_os.mappings["clinical_events"]["mappings"]["properties"]
    )  # additive update
    assert {SERIES_INDEX, STUDIES_INDEX, PATIENTS_INDEX, DLQ_INDEX, "audit"} <= set(fake_os.data)


def test_index_folder_and_rollups(
    store: IndexStore, fake_os: FakeOpenSearch, salt: bytes, tmp_cohort: Path
) -> None:
    result = _pipeline(store, salt).process(FolderSource(tmp_cohort, project="COHORT").pending())
    assert result.dead_lettered == 0
    series = fake_os.docs(SERIES_INDEX)
    studies = fake_os.docs(STUDIES_INDEX)
    patients = fake_os.docs(PATIENTS_INDEX)
    assert len(series) == 12 and len(studies) == 10 and len(patients) == 7
    assert all(s["instance_count"] == 2 for s in series)  # 2 slices per series collapsed
    assert all(s["deidentified"] for s in series)
    blob = str(series + studies + patients)
    for phi in ("MRN1000", "Doe", "Springfield", "1950-03-14"):
        assert phi not in blob
    garcia = [p for p in patients if p["age_latest"] == 90.0]
    assert len(garcia) == 1  # the >89 patient is capped
    multi = [p for p in patients if p["study_count"] == 2]
    assert len(multi) == 3


def test_reprocessing_is_idempotent(
    store: IndexStore, fake_os: FakeOpenSearch, salt: bytes, tmp_cohort: Path
) -> None:
    pipe = _pipeline(store, salt)
    source = FolderSource(tmp_cohort, project="COHORT")
    pipe.process(source.pending())
    first = {
        k: sorted(map(str, fake_os.docs(k))) for k in (SERIES_INDEX, STUDIES_INDEX, PATIENTS_INDEX)
    }
    counts = {k: len(v) for k, v in first.items()}
    assert source.pending() == []  # unchanged files are skipped
    pipe.process(source.pending(rescan=True))
    pipe.process(source.pending(rescan=True))
    assert {k: len(fake_os.docs(k)) for k in counts} == counts

    def strip(docs):
        return sorted(str({k: v for k, v in d.items() if k != "indexed_at"}) for d in docs)

    for index in counts:
        assert strip(fake_os.docs(index)) == strip([eval(d) for d in first[index]])  # noqa: S307


def test_dlq_routing(
    store: IndexStore, fake_os: FakeOpenSearch, salt: bytes, tmp_path: Path, tmp_cohort: Path
) -> None:
    for bad in write_corrupt(tmp_path / "bad"):
        (tmp_cohort / bad.name).write_bytes(bad.read_bytes())
    pipe = _pipeline(store, salt)
    result = pipe.process(FolderSource(tmp_cohort, project="COHORT").pending())
    assert result.dead_lettered == 2 and len(result.failed_refs) == 2
    dlq = fake_os.docs(DLQ_INDEX)
    assert {d["stage"] for d in dlq} <= {"extract", "deid"}
    assert len(fake_os.docs(SERIES_INDEX)) == 12
    # same bad file again -> same DLQ doc, attempts incremented
    pipe.process(FolderSource(tmp_cohort, project="COHORT").pending())
    dlq = fake_os.docs(DLQ_INDEX)
    assert len(dlq) == 2 and all(d["attempts"] == 2 for d in dlq)


def test_loader_failures_and_deid_rejection(
    store: IndexStore, fake_os: FakeOpenSearch, salt: bytes, make_ds
) -> None:  # type: ignore[no-untyped-def]
    from io import BytesIO

    sr = make_ds(Modality="SR")
    buf = BytesIO()
    sr.save_as(buf, enforce_file_format=True)

    def boom() -> bytes:
        raise OSError("disk gone")

    items = [
        WorkItem(ref="mem:sr", load=lambda: buf.getvalue()),
        WorkItem(ref="mem:boom", load=boom),
    ]
    result = _pipeline(store, salt).process(items)
    assert result.dead_lettered == 2
    assert {d["stage"] for d in fake_os.docs(DLQ_INDEX)} == {"deid", "read"}


def test_bulk_item_failures_go_to_dlq(
    store: IndexStore, fake_os: FakeOpenSearch, salt: bytes, tmp_cohort: Path
) -> None:
    source = FolderSource(tmp_cohort, project="COHORT")
    pipe = _pipeline(store, salt)
    items = source.pending()
    # Reject one series id at bulk time.
    pipe.process(items[:1])
    victim = fake_os.docs(SERIES_INDEX)[0]["series_uid"]
    fake_os.data[SERIES_INDEX].clear()
    fake_os.reject_ids.add(victim)
    result = pipe.process(source.pending(rescan=True))
    assert result.dead_lettered >= 1
    assert any(d["stage"] == "index" for d in fake_os.docs(DLQ_INDEX))


def test_bulk_outage_dead_letters_batch(
    store: IndexStore, fake_os: FakeOpenSearch, salt: bytes, tmp_cohort: Path
) -> None:
    pipe = _pipeline(store, salt)

    def explode(*a: object, **k: object) -> None:
        raise ConnectionError("cluster down")

    store.index_series = explode  # type: ignore[method-assign,assignment]
    result = pipe.process(FolderSource(tmp_cohort, project="COHORT").pending())
    assert result.indexed == 0 and result.dead_lettered == 12


def test_dlq_write_failure_is_swallowed(store: IndexStore, fake_os: FakeOpenSearch) -> None:
    def broken(*a: object, **k: object) -> None:
        raise ConnectionError("nope")

    fake_os.update = broken  # type: ignore[method-assign]
    assert store.dead_letter(ref="x", source="s", stage="read", error="E")


def test_count_and_empty_inputs(store: IndexStore) -> None:
    assert store.count("missing-index") == 0
    assert store.index_series([]) == (0, [])
    assert store.rollup_patients([]) == 0


def test_doc_builders() -> None:
    series = [
        {
            "series_uid": "1.2",
            "study_uid": "9",
            "patient_pseudonym": "SP1",
            "series_number": 2,
            "modality": "CT",
            "body_part": "CHEST",
            "instance_count": 3,
            "manufacturer": "GE",
            "xnat_experiment_id": "E1",
            "project": "P",
            "xnat_subject_id": "S1",
            "study_date": "2021-01-01",
        },
        {
            "series_uid": "1.1",
            "study_uid": "9",
            "patient_pseudonym": "SP1",
            "series_number": 1,
            "modality": "PT",
            "body_part": "CHEST",
            "instance_count": 5,
            "manufacturer": "GE",
        },
    ]
    study = build_study_doc("9", series, "https://x")
    assert study["modalities"] == ["PT", "CT"] and study["body_parts"] == ["CHEST"]
    assert study["instance_count"] == 8 and study["series_count"] == 2
    assert "experimentId=E1" in study["viewer_url"] and "subjectId=S1" in study["viewer_url"]
    assert build_study_doc("9", series)["viewer_url"] is None
    patient = build_patient_doc(
        "SP1",
        [study],
        [
            {
                "diagnosis_category": "LUNG_CANCER",
                "diagnosis_code": "C34.90",
                "patient_age": 70.0,
                "patient_sex": "M",
            }
        ],
    )
    assert patient["diagnoses"] == ["LUNG_CANCER"] and patient["age_latest"] == 70.0
    assert patient["patient_sex"] == "M" and patient["study_count"] == 1
