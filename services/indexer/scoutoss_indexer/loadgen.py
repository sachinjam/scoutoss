"""Synthetic index-only load for scale testing (``scoutoss-indexer loadtest``).

Generates de-identified-looking series documents and clinical events for N studies and indexes
them through the same :class:`IndexStore` code path real data uses (bulk index, then study and
patient roll-ups). No DICOM files, no XNAT and no pixel data are involved, so 10,000+ studies
load in minutes on a laptop. Everything goes into its own project (default ``LOADTEST``) so it
never mixes with real projects and can be removed with ``--purge``.

The data is deterministic for a given seed. Distributions are rough but plausible (CT/MR heavy,
ages 20-90, a third of patients with a coded diagnosis) so that copilot questions select
realistic fractions of the data lake.
"""

from __future__ import annotations

import hashlib
import random
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from opensearchpy import OpenSearch, helpers

from scoutoss_common.catalog import (
    CLINICAL_INDEX,
    PATIENTS_INDEX,
    PROJECT_FIELD,
    SERIES_INDEX,
    STUDIES_INDEX,
)
from scoutoss_indexer.extract import SeriesRecord
from scoutoss_indexer.store import IndexStore

DEFAULT_PROJECT = "LOADTEST"

# (modality, weight, body parts, manufacturers, SOP class)
MODALITIES: list[tuple[str, int, list[str], list[str], str]] = [
    (
        "CT",
        45,
        ["CHEST", "ABDOMEN", "HEAD", "PELVIS"],
        ["SIEMENS", "GE MEDICAL SYSTEMS", "Philips"],
        "1.2.840.10008.5.1.4.1.1.2",
    ),
    (
        "MR",
        30,
        ["HEAD", "KNEE", "SPINE", "ABDOMEN"],
        ["SIEMENS", "GE MEDICAL SYSTEMS", "Philips"],
        "1.2.840.10008.5.1.4.1.1.4",
    ),
    (
        "CR",
        15,
        ["CHEST", "KNEE", "PELVIS"],
        ["FUJIFILM", "Carestream"],
        "1.2.840.10008.5.1.4.1.1.1",
    ),
    ("PT", 6, ["WHOLEBODY"], ["SIEMENS", "GE MEDICAL SYSTEMS"], "1.2.840.10008.5.1.4.1.1.128"),
    (
        "US",
        4,
        ["ABDOMEN", "PELVIS"],
        ["GE MEDICAL SYSTEMS", "Philips"],
        "1.2.840.10008.5.1.4.1.1.6.1",
    ),
]
# (ICD-10 code, text, category, weight)
DIAGNOSES: list[tuple[str, str, str, int]] = [
    ("C34.90", "Malignant neoplasm of unspecified part of bronchus or lung", "LUNG_CANCER", 20),
    ("C50.919", "Malignant neoplasm of unspecified site of breast", "BREAST_CANCER", 15),
    ("I63.9", "Cerebral infarction, unspecified", "STROKE", 20),
    ("J18.9", "Pneumonia, unspecified organism", "PNEUMONIA", 25),
    ("M17.9", "Osteoarthritis of knee, unspecified", "OSTEOARTHRITIS", 20),
]
SITES = ["SITE-A1B2C3", "SITE-D4E5F6", "SITE-0A9B8C"]
STUDIES_PER_PATIENT = 1.6


@dataclass
class LoadStats:
    studies: int = 0
    series: int = 0
    patients: int = 0
    clinical_events: int = 0
    failed: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        rate = round(self.studies / self.seconds, 1) if self.seconds else None
        return {**self.__dict__, "seconds": round(self.seconds, 1), "studies_per_second": rate}


def _hex(*parts: object) -> str:
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()


def _uid(*parts: object) -> str:
    return f"2.25.{int(_hex(*parts)[:30], 16)}"


def _pseudonym(seed: int, n: int) -> str:
    return "SP" + _hex("patient", seed, n)[:24].upper()


def _pick(rng: random.Random, weighted: list[tuple[Any, int]]) -> Any:
    return rng.choices([w[0] for w in weighted], weights=[w[1] for w in weighted])[0]


def generate(
    studies: int, *, project: str = DEFAULT_PROJECT, seed: int = 7
) -> Iterator[tuple[list[SeriesRecord], list[dict[str, Any]]]]:
    """Yield ``(series records, clinical event docs)`` per study, deterministically."""
    rng = random.Random(seed)
    patients = max(1, round(studies / STUDIES_PER_PATIENT))
    with_events: set[int] = set()
    start = date(2015, 1, 1)
    now = datetime.now(UTC).isoformat()
    for n in range(studies):
        patient = rng.randrange(patients)
        prng = random.Random(seed * 1_000_003 + patient)
        sex = prng.choice(["M", "F"])
        birth_year = prng.randint(1932, 2002)
        pseudonym = _pseudonym(seed, patient)
        modality, _, parts, makers, sop = rng.choices(
            MODALITIES, weights=[m[1] for m in MODALITIES]
        )[0]
        body_part = rng.choice(parts)
        study_date = start + timedelta(days=rng.randrange(365 * 9))
        age = float(min(90, max(0, study_date.year - birth_year)))
        study_uid = _uid("study", seed, n)
        maker = rng.choice(makers)
        description = f"{modality} {body_part}"
        records = [
            SeriesRecord(
                series_uid=_uid("series", seed, n, s),
                study_uid=study_uid,
                patient_pseudonym=pseudonym,
                modality=modality,
                body_part=body_part,
                body_part_raw=body_part,
                series_description=f"{description} series {s + 1}",
                series_number=s + 1,
                study_description=description,
                study_date=study_date.isoformat(),
                study_year=study_date.year,
                patient_sex=sex,
                patient_age=age,
                manufacturer=maker,
                manufacturer_model=f"{maker.split()[0]} Model {1 + n % 4}",
                institution=rng.choice(SITES),
                sop_class_uid=sop,
                slice_thickness=rng.choice([1.0, 2.5, 5.0]) if modality in ("CT", "MR") else None,
                magnetic_field_strength=rng.choice([1.5, 3.0]) if modality == "MR" else None,
                project=project,
                xnat_experiment_id=f"{project}_E{n:07d}",
                xnat_session_label=f"{pseudonym}_{modality}_{n:07d}",
                xnat_scan_id=str(s + 1),
                xnat_subject_id=f"{project}_S{patient:07d}",
                instance_count=rng.choice([1, 40, 120, 300]),
                deidentified=True,
                source="loadtest",
                indexed_at=now,
            )
            for s in range(rng.choice([1, 1, 2, 3, 4]))
        ]
        events: list[dict[str, Any]] = []
        if patient not in with_events and prng.random() < 0.34:
            with_events.add(patient)
            code, text, category, _ = prng.choices(DIAGNOSES, weights=[d[3] for d in DIAGNOSES])[0]
            events.append(
                {
                    "patient_pseudonym": pseudonym,
                    "project": project,
                    "message_type": "ADT^A01",
                    "event_date": study_date.isoformat(),
                    "patient_sex": sex,
                    "patient_age": age,
                    "source": "loadtest",
                    "indexed_at": now,
                    "event_id": _hex("event", seed, patient)[:32],
                    "event_type": "diagnosis",
                    "diagnosis_code": code,
                    "diagnosis_system": "I10",
                    "diagnosis_text": text,
                    "diagnosis_category": category,
                }
            )
        yield records, events


def _check_project(project: str) -> None:
    """Load-test data may only be written to, or purged from, a LOADTEST* project, so a typo
    can never fill or wipe a real project's index documents."""
    if not project.upper().startswith("LOADTEST"):
        raise ValueError("load-test project names must start with LOADTEST")


def load(
    store: IndexStore,
    studies: int,
    *,
    project: str = DEFAULT_PROJECT,
    seed: int = 7,
    batch: int = 1000,
) -> LoadStats:
    """Index ``studies`` synthetic studies in batches: series, clinical events, then roll-ups."""
    _check_project(project)
    stats = LoadStats()
    started = time.monotonic()
    patients: set[str] = set()
    records: list[SeriesRecord] = []
    events: list[dict[str, Any]] = []
    study_uids: set[str] = set()

    def flush() -> None:
        if not records:
            return
        ok, failed = store.index_series(records)
        stats.series += ok
        stats.failed += len(failed)
        if events:
            done, errors = helpers.bulk(
                store.client,
                (
                    {
                        "_op_type": "index",
                        "_index": CLINICAL_INDEX,
                        "_id": e["event_id"],
                        "_source": e,
                    }
                    for e in events
                ),
                raise_on_error=False,
                refresh=True,
            )
            stats.clinical_events += int(done)
            stats.failed += len(errors) if isinstance(errors, list) else int(errors)
        touched = store.rollup_studies(study_uids)
        store.rollup_patients(touched)
        patients.update(touched)
        stats.studies += len(study_uids)
        records.clear()
        events.clear()
        study_uids.clear()

    for series, clinical in generate(studies, project=project, seed=seed):
        records.extend(series)
        events.extend(clinical)
        study_uids.add(series[0].study_uid)
        if len(study_uids) >= batch:
            flush()
    flush()
    stats.patients = len(patients)
    stats.seconds = time.monotonic() - started
    return stats


def purge(client: OpenSearch, project: str = DEFAULT_PROJECT) -> dict[str, int]:
    """Remove every load-test document of ``project`` from the data-lake indices."""
    _check_project(project)
    deleted: dict[str, int] = {}
    for index in (SERIES_INDEX, STUDIES_INDEX, PATIENTS_INDEX, CLINICAL_INDEX):
        resp = client.delete_by_query(
            index=index,
            body={"query": {"term": {PROJECT_FIELD[index]: project}}},
            refresh=True,
            conflicts="proceed",
            ignore_unavailable=True,
        )
        deleted[index] = int(resp.get("deleted", 0))
    return deleted
