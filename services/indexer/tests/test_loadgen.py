"""Synthetic index-only load generator."""

from __future__ import annotations

import pytest

from scoutoss_common.catalog import CLINICAL_INDEX, PATIENTS_INDEX, SERIES_INDEX, STUDIES_INDEX
from scoutoss_indexer.loadgen import generate, load, purge
from scoutoss_indexer.store import IndexStore
from tests.support.fake_opensearch import FakeOpenSearch


@pytest.fixture
def store(fake_os: FakeOpenSearch) -> IndexStore:
    s = IndexStore(fake_os, xnat_public_url="https://xnat.example")  # type: ignore[arg-type]
    s.ensure_indices()
    return s


def test_generation_is_deterministic_and_deidentified_looking() -> None:
    first = list(generate(50))
    again = list(generate(50))
    assert [r.series_uid for rs, _ in first for r in rs] == [
        r.series_uid for rs, _ in again for r in rs
    ]
    assert len({rs[0].study_uid for rs, _ in first}) == 50
    for records, events in first:
        for r in records:
            assert r.project == "LOADTEST" and r.deidentified and r.source == "loadtest"
            assert r.patient_pseudonym.startswith("SP") and len(r.patient_pseudonym) == 26
            assert r.study_uid.startswith("2.25.") and 0 <= (r.patient_age or 0) <= 90
        assert all(e["diagnosis_category"] and e["project"] == "LOADTEST" for e in events)
    assert list(generate(5, seed=1)) != list(generate(5, seed=2))


def test_load_indexes_rolls_up_and_purges_only_its_project(
    store: IndexStore, fake_os: FakeOpenSearch
) -> None:
    fake_os.index(index=STUDIES_INDEX, id="real", body={"study_uid": "real", "project": "REAL"})
    stats = load(store, 120, batch=50)
    assert stats.studies == 120 and stats.failed == 0 and stats.series >= 120
    assert len(fake_os.docs(SERIES_INDEX)) == stats.series
    assert len(fake_os.docs(STUDIES_INDEX)) == 121
    patients = fake_os.docs(PATIENTS_INDEX)
    assert len(patients) == stats.patients and sum(p["study_count"] for p in patients) == 120
    assert len(fake_os.docs(CLINICAL_INDEX)) == stats.clinical_events > 0
    assert stats.as_dict()["studies_per_second"]

    load(store, 120, batch=50)  # idempotent: same seed, same documents
    assert len(fake_os.docs(STUDIES_INDEX)) == 121

    deleted = purge(fake_os)  # type: ignore[arg-type]
    assert deleted[STUDIES_INDEX] == 120 and deleted[SERIES_INDEX] == stats.series
    assert [d["study_uid"] for d in fake_os.docs(STUDIES_INDEX)] == ["real"]
    assert fake_os.docs(PATIENTS_INDEX) == [] and fake_os.docs(CLINICAL_INDEX) == []


def test_only_loadtest_projects_can_be_loaded_or_purged(
    store: IndexStore, fake_os: FakeOpenSearch
) -> None:
    with pytest.raises(ValueError, match="LOADTEST"):
        load(store, 1, project="SCOUT_DEMO")
    with pytest.raises(ValueError, match="LOADTEST"):
        purge(fake_os, "SCOUT_DEMO")  # type: ignore[arg-type]
