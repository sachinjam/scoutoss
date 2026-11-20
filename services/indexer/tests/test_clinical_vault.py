from __future__ import annotations

import pytest

from scoutoss_indexer.clinical import (
    ClinicalEvent,
    ClinicalIngestor,
    MemoryClinicalRepo,
    categorize,
)
from scoutoss_indexer.deid.pseudonym import pseudonymize
from scoutoss_indexer.deid.vault import Cipher, MemoryVault
from scoutoss_indexer.store import IndexStore
from tests.support.fake_opensearch import FakeOpenSearch


def _event(**kw: object) -> ClinicalEvent:
    base: dict[str, object] = {
        "message_control_id": "M1",
        "message_type": "ORU^R01",
        "patient_id": "MRN100004",
        "patient_sex": "male",
        "birth_date": "19440130",
        "event_datetime": "202203011200",
        "observations": [
            {"code": "2160-0", "name": "Creatinine", "value": "1.3", "units": "mg/dL", "flag": "H"},
            {"code": "TXT", "name": "Comment for John Doe", "value": "see note 555-867-5309"},
        ],
        "orders": [{"code": "CTCH", "text": "CT chest"}],
        "diagnoses": [{"code": "C34.1", "text": "lung adenocarcinoma"}, {"text": "pneumonia"}],
    }
    base.update(kw)
    return ClinicalEvent.model_validate(base)


def test_categorize() -> None:
    assert categorize("C34.90", None) == "LUNG_CANCER"
    assert categorize("c509", None) == "BREAST_CANCER"
    assert categorize(None, "Non-small cell lung carcinoma") == "LUNG_CANCER"
    assert categorize(None, "Carcinoma of the bronchus") == "LUNG_CANCER"
    assert categorize("Z00", "routine") is None
    assert categorize(None, None) is None


def test_ingest_pseudonymizes_and_joins(fake_os: FakeOpenSearch, salt: bytes) -> None:
    store = IndexStore(fake_os)  # type: ignore[arg-type]
    store.ensure_indices()
    repo = MemoryClinicalRepo()
    vault = MemoryVault({"SCOUT_DEMO": -10})
    ing = ClinicalIngestor(store, repo, salt=salt, vault=vault, default_project="SCOUT_DEMO")
    out = ing.ingest([_event(), _event()])  # duplicate message -> same event ids
    assert out == {"messages": 2, "documents": 10, "patients": 1}
    docs = fake_os.docs("clinical_events")
    assert len(docs) == 5 and len(repo.events) == 5
    pseudonym = pseudonymize("MRN100004", salt)
    assert {d["patient_pseudonym"] for d in docs} == {pseudonym}
    assert vault.subjects[pseudonym][0] == "MRN100004"
    blob = str(docs) + str(repo.events)
    for phi in ("MRN100004", "John Doe", "555-867-5309", "19440130"):
        assert phi not in blob
    lab = next(d for d in docs if d.get("lab_code") == "2160-0")
    assert lab["lab_value"] == 1.3 and lab["event_date"] == "2022-02-19"
    assert lab["patient_age"] == pytest.approx(78.08, abs=0.01) and lab["patient_sex"] == "M"
    text_lab = next(d for d in docs if d.get("lab_code") == "TXT")
    assert text_lab["lab_value"] is None and "[PHONE]" in text_lab["lab_value_text"]
    patient = fake_os.docs("dicom_patients")[0]
    assert patient["diagnoses"] == ["LUNG_CANCER", "PNEUMONIA"]


def test_event_without_details_and_validation(fake_os: FakeOpenSearch, salt: bytes) -> None:
    store = IndexStore(fake_os)  # type: ignore[arg-type]
    store.ensure_indices()
    ing = ClinicalIngestor(
        store, MemoryClinicalRepo(), salt=salt, vault=MemoryVault(), default_project="P"
    )
    ev = ClinicalEvent(
        message_control_id="A", message_type="adt_a08", patient_id="X1", patient_sex="Q"
    )
    _, sex, docs = ing.to_docs(ev)
    assert sex == "U" and docs[0]["event_type"] == "update" and docs[0]["event_date"] is None
    with pytest.raises(ValueError, match="unsupported"):
        ClinicalEvent(message_control_id="A", message_type="ADT^A99", patient_id="X")
    assert "patient_id" not in repr(ev)


def test_cipher_roundtrip_and_binding(vault_key: bytes) -> None:
    c = Cipher(vault_key)
    blob = c.encrypt("MRN100001", "subject:SP1")
    assert b"MRN100001" not in blob
    assert c.decrypt(blob, "subject:SP1") == "MRN100001"
    with pytest.raises(Exception):  # noqa: B017,PT011 - InvalidTag
        c.decrypt(blob, "subject:SP2")
    with pytest.raises(ValueError, match="32 bytes"):
        Cipher(b"short")


def test_memory_vault_offsets_stable() -> None:
    v = MemoryVault()
    first = v.project_offset("A")
    assert v.project_offset("A") == first and -730 <= first <= -30


def test_index_failures_are_dead_lettered_and_raised(fake_os: FakeOpenSearch, salt: bytes) -> None:
    from scoutoss_indexer.clinical import ClinicalIngestError

    store = IndexStore(fake_os)  # type: ignore[arg-type]
    store.ensure_indices()
    fake_os.fail_index.add("clinical_events")
    ing = ClinicalIngestor(
        store, MemoryClinicalRepo(), salt=salt, vault=MemoryVault(), default_project="P"
    )
    with pytest.raises(ClinicalIngestError, match="failed to index"):
        ing.ingest([_event()])
    assert {d["stage"] for d in fake_os.docs("indexer_dlq")} == {"index"}
