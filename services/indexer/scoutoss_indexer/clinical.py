"""HL7v2 clinical ingest (ADT^A01/A08, ORM^O01, ORU^R01).

Mirth Connect parses each HL7 message into the JSON shape of :class:`ClinicalEvent`
and POSTs it here. This module is the only place that sees the raw MRN: it pseudonymizes
with the same HMAC salt as the DICOM gateway (so records join on ``patient_pseudonym``),
shifts dates with the project offset, computes age-at-event, scrubs free text, then writes
the ``clinical`` Postgres schema and the ``clinical_events`` OpenSearch index.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import structlog
from opensearchpy import helpers
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field, field_validator

from scoutoss_common.catalog import CLINICAL_INDEX
from scoutoss_common.phi import redact_text
from scoutoss_indexer.deid.dates import age_years, cap_age, parse_da
from scoutoss_indexer.deid.pseudonym import pseudonymize
from scoutoss_indexer.deid.vault import Vault
from scoutoss_indexer.store import DOCS_INDEXED, IndexStore

log = structlog.get_logger(__name__)

DDL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'clinical') THEN
        CREATE SCHEMA clinical;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS clinical.patients (
    patient_pseudonym text PRIMARY KEY,
    patient_sex       text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS clinical.events (
    event_id          text PRIMARY KEY,
    patient_pseudonym text NOT NULL REFERENCES clinical.patients(patient_pseudonym),
    message_type      text NOT NULL,
    event_type        text NOT NULL,
    event_date        date,
    detail            jsonb NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_patient_idx ON clinical.events (patient_pseudonym);
"""

MESSAGE_TYPES = {"ADT^A01", "ADT^A08", "ORM^O01", "ORU^R01"}

# ICD-10 prefix -> category. Longest prefix wins.
ICD10_CATEGORIES: dict[str, str] = {
    "C34": "LUNG_CANCER",
    "C33": "LUNG_CANCER",
    "C50": "BREAST_CANCER",
    "C61": "PROSTATE_CANCER",
    "C18": "COLORECTAL_CANCER",
    "C19": "COLORECTAL_CANCER",
    "C20": "COLORECTAL_CANCER",
    "C25": "PANCREATIC_CANCER",
    "C71": "BRAIN_CANCER",
    "C22": "LIVER_CANCER",
    "C64": "KIDNEY_CANCER",
    "C73": "THYROID_CANCER",
    "C81": "LYMPHOMA",
    "C82": "LYMPHOMA",
    "C83": "LYMPHOMA",
    "C85": "LYMPHOMA",
    "R91": "LUNG_NODULE",
    "I63": "STROKE",
    "I21": "MYOCARDIAL_INFARCTION",
    "I50": "HEART_FAILURE",
    "J18": "PNEUMONIA",
    "J44": "COPD",
    "E11": "DIABETES_T2",
    "U07": "COVID19",
}
_TEXT_CATEGORIES: tuple[tuple[str, str], ...] = (
    (
        r"lung (cancer|carcinoma|adenocarcinoma)|(carcinoma|cancer|neoplasm) of (the )?(lung|bronch)|nsclc|sclc",
        "LUNG_CANCER",
    ),
    (r"breast (cancer|carcinoma)", "BREAST_CANCER"),
    (r"prostate (cancer|carcinoma|adenocarcinoma)", "PROSTATE_CANCER"),
    (r"(colon|colorectal|rectal) (cancer|carcinoma)", "COLORECTAL_CANCER"),
    (r"glioblastoma|glioma|brain (tumou?r|cancer)", "BRAIN_CANCER"),
    (r"pulmonary nodule|lung nodule", "LUNG_NODULE"),
    (r"stroke|cerebral infarction", "STROKE"),
    (r"pneumonia", "PNEUMONIA"),
)

EVENT_TYPES = {
    "ADT^A01": "admission",
    "ADT^A08": "update",
    "ORM^O01": "order",
    "ORU^R01": "result",
}


def categorize(code: str | None, text: str | None) -> str | None:
    if code:
        normalized = code.upper().replace(".", "")
        for length in (4, 3):
            cat = ICD10_CATEGORIES.get(normalized[:length])
            if cat:
                return cat
    if text:
        lowered = text.lower()
        for pattern, cat in _TEXT_CATEGORIES:
            if re.search(pattern, lowered):
                return cat
    return None


class Diagnosis(BaseModel):
    code: str | None = Field(default=None, max_length=32)
    system: str | None = Field(default=None, max_length=32)
    text: str | None = Field(default=None, max_length=512)


class Order(BaseModel):
    code: str | None = Field(default=None, max_length=64)
    text: str | None = Field(default=None, max_length=512)


class Observation(BaseModel):
    code: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=256)
    value: str | None = Field(default=None, max_length=256)
    units: str | None = Field(default=None, max_length=64)
    flag: str | None = Field(default=None, max_length=8)


class ClinicalEvent(BaseModel):
    """One parsed HL7 message (contains PHI on input — never logged)."""

    message_control_id: str = Field(max_length=64)
    message_type: str = Field(max_length=16)
    patient_id: str = Field(min_length=1, max_length=64, repr=False)
    patient_sex: str | None = Field(default=None, max_length=8)
    birth_date: str | None = Field(default=None, max_length=32, repr=False)
    event_datetime: str | None = Field(default=None, max_length=32)
    project: str | None = Field(default=None, max_length=64)
    diagnoses: list[Diagnosis] = Field(default_factory=list, max_length=50)
    orders: list[Order] = Field(default_factory=list, max_length=50)
    observations: list[Observation] = Field(default_factory=list, max_length=200)

    @field_validator("message_type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        v = v.strip().upper().replace("_", "^")
        if v not in MESSAGE_TYPES:
            raise ValueError(f"unsupported message type {v}")
        return v


class ClinicalIngestError(RuntimeError):
    """Some documents could not be indexed (they are in the DLQ); the sender should retry."""


class ClinicalRepo(Protocol):
    def save(self, patients: dict[str, str | None], events: list[dict[str, Any]]) -> None: ...


class MemoryClinicalRepo:
    def __init__(self) -> None:
        self.patients: dict[str, str | None] = {}
        self.events: dict[str, dict[str, Any]] = {}

    def save(self, patients: dict[str, str | None], events: list[dict[str, Any]]) -> None:
        self.patients.update(patients)
        for e in events:
            self.events[e["event_id"]] = e


class PostgresClinicalRepo:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def migrate(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(DDL)

    def save(self, patients: dict[str, str | None], events: list[dict[str, Any]]) -> None:
        with self.pool.connection() as conn, conn.transaction():
            for pseudonym, sex in patients.items():
                conn.execute(
                    "INSERT INTO clinical.patients (patient_pseudonym, patient_sex) VALUES (%s, %s) "
                    "ON CONFLICT (patient_pseudonym) DO UPDATE SET "
                    "patient_sex = COALESCE(EXCLUDED.patient_sex, clinical.patients.patient_sex), "
                    "updated_at = now()",
                    (pseudonym, sex),
                )
            for e in events:
                conn.execute(
                    "INSERT INTO clinical.events (event_id, patient_pseudonym, message_type, "
                    "event_type, event_date, detail) VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (event_id) DO UPDATE SET detail = EXCLUDED.detail",
                    (
                        e["event_id"],
                        e["patient_pseudonym"],
                        e["message_type"],
                        e["event_type"],
                        e.get("event_date"),
                        Jsonb(e),
                    ),
                )


def _parse_hl7_ts(value: str | None) -> date | None:
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", value)[:8]
    return parse_da(digits) if len(digits) == 8 else None


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


class ClinicalIngestor:
    def __init__(
        self,
        store: IndexStore,
        repo: ClinicalRepo,
        *,
        salt: bytes,
        vault: Vault,
        default_project: str,
    ) -> None:
        self.store = store
        self.repo = repo
        self.salt = salt
        self.vault = vault
        self.default_project = default_project

    def to_docs(self, event: ClinicalEvent) -> tuple[str, str | None, list[dict[str, Any]]]:
        project = event.project or self.default_project
        offset = self.vault.project_offset(project)
        pseudonym = pseudonymize(event.patient_id, self.salt)
        self.vault.record_subject(pseudonym, event.patient_id, project)
        sex = (event.patient_sex or "").strip().upper()[:1] or None
        if sex and sex not in {"M", "F", "O"}:
            sex = "U"
        when = _parse_hl7_ts(event.event_datetime)
        birth = _parse_hl7_ts(event.birth_date)
        age = cap_age(age_years(birth, when)) if birth and when and when >= birth else None
        shifted = (when + timedelta(days=offset)).isoformat() if when else None
        base = {
            "patient_pseudonym": pseudonym,
            "project": project,
            "message_type": event.message_type,
            "event_date": shifted,
            "patient_sex": sex,
            "patient_age": age,
            "source": "hl7",
            "indexed_at": datetime.now(UTC).isoformat(),
        }

        def ident(kind: str, i: int) -> str:
            raw = f"{event.message_control_id}|{event.message_type}|{kind}|{i}|{pseudonym}"
            return hashlib.sha256(raw.encode()).hexdigest()[:32]

        docs: list[dict[str, Any]] = []
        for i, dx in enumerate(event.diagnoses):
            text = redact_text(dx.text) if dx.text else None
            docs.append(
                {
                    **base,
                    "event_id": ident("dx", i),
                    "event_type": "diagnosis",
                    "diagnosis_code": dx.code,
                    "diagnosis_system": dx.system,
                    "diagnosis_text": text,
                    "diagnosis_category": categorize(dx.code, dx.text),
                }
            )
        for i, order in enumerate(event.orders):
            docs.append(
                {
                    **base,
                    "event_id": ident("order", i),
                    "event_type": "order",
                    "order_code": order.code,
                    "order_text": redact_text(order.text) if order.text else None,
                }
            )
        for i, obs in enumerate(event.observations):
            docs.append(
                {
                    **base,
                    "event_id": ident("obs", i),
                    "event_type": "result",
                    "lab_code": obs.code,
                    "lab_name": redact_text(obs.name) if obs.name else None,
                    "lab_value": _float(obs.value),
                    "lab_value_text": None
                    if _float(obs.value) is not None
                    else (redact_text(obs.value)[:64] if obs.value else None),
                    "lab_units": obs.units,
                    "lab_flag": obs.flag,
                }
            )
        if not docs:
            docs.append(
                {**base, "event_id": ident("evt", 0), "event_type": EVENT_TYPES[event.message_type]}
            )
        return pseudonym, sex, docs

    def ingest(self, events: list[ClinicalEvent]) -> dict[str, int]:
        patients: dict[str, str | None] = {}
        docs: list[dict[str, Any]] = []
        for event in events:
            pseudonym, sex, event_docs = self.to_docs(event)
            patients[pseudonym] = sex or patients.get(pseudonym)
            docs.extend(event_docs)
        self.repo.save(patients, docs)
        actions = [
            {"_op_type": "index", "_index": CLINICAL_INDEX, "_id": d["event_id"], "_source": d}
            for d in docs
        ]
        ok, errors = helpers.bulk(
            self.store.client, actions, refresh=True, raise_on_error=False, raise_on_exception=False
        )
        DOCS_INDEXED.labels(CLINICAL_INDEX).inc(int(ok))
        if errors:
            for failure in errors:
                info: dict[str, Any] = (
                    next(iter(failure.values()), {}) if isinstance(failure, dict) else {}
                )
                self.store.dead_letter(
                    ref=f"hl7:{info.get('_id', '?')}",
                    source="hl7",
                    stage="index",
                    error=str(info.get("error", {}).get("type", "index_error")),
                )
            raise ClinicalIngestError(f"{len(errors)} clinical document(s) failed to index")
        self.store.rollup_patients(patients.keys())
        log.info(
            "clinical events ingested", messages=len(events), docs=len(docs), patients=len(patients)
        )
        return {"messages": len(events), "documents": len(docs), "patients": len(patients)}
