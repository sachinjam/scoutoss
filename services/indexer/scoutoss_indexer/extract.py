"""DICOM header -> normalized, de-identified series record.

Handles the messy reality of DICOM headers: missing tags (-> ``None``), multi-valued
strings where single values are expected, non-ASCII character sets, and malformed values
(unparseable dates/ages become ``None`` rather than raising). Only missing *identity*
(Study/Series UID) is fatal, raised as :class:`ExtractionError` and routed to the DLQ.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pydicom
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError
from pydicom.multival import MultiValue

from scoutoss_indexer.deid.dates import age_years, cap_age, parse_da, parse_dicom_age
from scoutoss_indexer.deid.deidentify import is_scoutoss_deidentified
from scoutoss_indexer.deid.pseudonym import is_pseudonym, pseudonymize, site_code

UID_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")

BODY_PART_SYNONYMS: dict[str, str] = {
    "THORAX": "CHEST",
    "LUNG": "CHEST",
    "LUNGS": "CHEST",
    "CHEST_TO_PELVIS": "CHESTABDPELVIS",
    "CAP": "CHESTABDPELVIS",
    "ABD": "ABDOMEN",
    "ABDOMENPELVIS": "ABDOMENPELVIS",
    "ABDPELVIS": "ABDOMENPELVIS",
    "BRAIN": "HEAD",
    "SKULL": "HEAD",
    "CRANIUM": "HEAD",
    "HEADNECK": "HEADNECK",
    "NECK": "NECK",
    "CSPINE": "CSPINE",
    "TSPINE": "TSPINE",
    "LSPINE": "LSPINE",
    "BREAST": "BREAST",
    "MAMMO": "BREAST",
    "PELVIS": "PELVIS",
    "PROSTATE": "PELVIS",
    "KNEE": "KNEE",
    "HEART": "HEART",
    "CARDIAC": "HEART",
    "WHOLEBODY": "WHOLEBODY",
    "WB": "WHOLEBODY",
}

# Ordered: first match wins when inferring from a description.
_DESCRIPTION_HINTS: tuple[tuple[str, str], ...] = (
    (r"CHEST.{0,4}ABD.{0,6}PELVIS|\bCAP\b", "CHESTABDPELVIS"),
    (r"ABD.{0,6}PELVIS", "ABDOMENPELVIS"),
    (r"CHEST|THORAX|LUNG|PULM", "CHEST"),
    (r"ABDOM|LIVER|PANCREA|\bABD\b", "ABDOMEN"),
    (r"BRAIN|HEAD|CRANI|SKULL", "HEAD"),
    (r"PELVIS|PROSTATE", "PELVIS"),
    (r"BREAST|MAMMO", "BREAST"),
    (r"SPINE|LUMBAR|CERVICAL|THORACIC", "SPINE"),
    (r"KNEE", "KNEE"),
    (r"CARDIAC|HEART|CORONARY", "HEART"),
    (r"WHOLE ?BODY", "WHOLEBODY"),
)


class ExtractionError(ValueError):
    """Header cannot produce an index record (missing identity, not DICOM, ...)."""


@dataclass
class SeriesRecord:
    series_uid: str
    study_uid: str
    patient_pseudonym: str
    modality: str | None
    body_part: str | None
    body_part_raw: str | None
    series_description: str | None
    series_number: int | None
    study_description: str | None
    study_date: str | None  # ISO yyyy-mm-dd (already shifted upstream)
    study_year: int | None
    patient_sex: str | None
    patient_age: float | None
    manufacturer: str | None
    manufacturer_model: str | None
    institution: str | None
    sop_class_uid: str | None
    slice_thickness: float | None
    magnetic_field_strength: float | None
    project: str | None = None
    xnat_experiment_id: str | None = None
    xnat_session_label: str | None = None
    xnat_scan_id: str | None = None
    xnat_subject_id: str | None = None
    instance_count: int = 1
    deidentified: bool = False
    source: str = "folder"
    indexed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_doc(self) -> dict[str, Any]:
        return asdict(self)


def read_header(data: bytes | Path) -> Dataset:
    """Parse a DICOM header (pixel data skipped). Raises :class:`ExtractionError`."""
    try:
        if isinstance(data, Path):
            return pydicom.dcmread(str(data), stop_before_pixels=True)
        return pydicom.dcmread(BytesIO(data), stop_before_pixels=True)
    except InvalidDicomError as exc:
        raise ExtractionError("not a DICOM file") from exc
    except Exception as exc:  # truncated / corrupt headers raise assorted errors
        raise ExtractionError(f"unreadable DICOM header: {type(exc).__name__}") from exc


def text(ds: Dataset, keyword: str) -> str | None:
    """Return a clean string value; multi-values are joined with ``\\``."""
    try:
        value = ds.get(keyword)
    except Exception:  # malformed element value
        return None
    if value is None:
        return None
    if isinstance(value, MultiValue | list):
        parts = [str(v).strip() for v in value if str(v).strip()]
        joined = "\\".join(parts)
        return joined or None
    result = str(value).replace("\x00", "").strip()
    return result or None


def first(ds: Dataset, keyword: str) -> str | None:
    value = text(ds, keyword)
    return value.split("\\")[0] if value else None


def number(ds: Dataset, keyword: str) -> float | None:
    raw = first(ds, keyword)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def normalize_body_part(raw: str | None, *descriptions: str | None) -> str | None:
    if raw:
        key = re.sub(r"[^A-Z]", "", raw.upper())
        if key:
            return BODY_PART_SYNONYMS.get(key, key)
    blob = " ".join(d.upper() for d in descriptions if d)
    for pattern, part in _DESCRIPTION_HINTS:
        if blob and re.search(pattern, blob):
            return part
    return None


def normalize_sex(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip().upper()[:1]
    return value if value in {"M", "F", "O"} else "U"


def derive_age(ds: Dataset, study: datetime | None) -> float | None:
    age = parse_dicom_age(first(ds, "PatientAge"))
    if age is not None:
        return age
    birth = parse_da(first(ds, "PatientBirthDate"))
    if birth and study and study.date() >= birth:
        return cap_age(age_years(birth, study.date()))
    return None


def extract_series(
    ds: Dataset,
    *,
    salt: bytes,
    instance_count: int = 1,
    context: dict[str, Any] | None = None,
) -> SeriesRecord:
    """Build a :class:`SeriesRecord`. Raw PatientIDs are pseudonymized here as a
    defense-in-depth measure if the object did not pass through the gateway."""
    study_uid = first(ds, "StudyInstanceUID")
    series_uid = first(ds, "SeriesInstanceUID")
    if not study_uid or not UID_RE.match(study_uid) or len(study_uid) > 64:
        raise ExtractionError("missing or invalid StudyInstanceUID")
    if not series_uid or not UID_RE.match(series_uid) or len(series_uid) > 64:
        raise ExtractionError("missing or invalid SeriesInstanceUID")

    deidentified = is_scoutoss_deidentified(ds)
    patient_id = first(ds, "PatientID")
    ctx = context or {}
    # The gateway writes the pseudonym into PatientID and PatientName, and XNAT uses it as
    # the subject label; any of them is acceptable if a later step rewrote PatientID.
    trusted = next(
        (
            c
            for c in (patient_id, first(ds, "PatientName"), ctx.get("subject_label"))
            if is_pseudonym(c)
        ),
        None,
    )
    if deidentified and trusted:
        pseudonym = str(trusted)
    elif patient_id:
        pseudonym = pseudonymize(patient_id, salt)
    else:
        pseudonym = pseudonymize(f"UNKNOWN:{study_uid}", salt)

    da = parse_da(first(ds, "StudyDate") or first(ds, "SeriesDate") or first(ds, "AcquisitionDate"))
    study_dt = datetime(da.year, da.month, da.day, tzinfo=UTC) if da else None

    institution = first(ds, "InstitutionName")
    if institution and not (deidentified and institution.startswith("SITE-")):
        institution = site_code(institution, salt)

    series_description = text(ds, "SeriesDescription")
    study_description = text(ds, "StudyDescription")
    body_raw = text(ds, "BodyPartExamined")
    series_number = number(ds, "SeriesNumber")

    return SeriesRecord(
        series_uid=series_uid,
        study_uid=study_uid,
        patient_pseudonym=pseudonym,
        modality=(first(ds, "Modality") or "").upper() or None,
        body_part=normalize_body_part(
            body_raw.split("\\")[0] if body_raw else None, study_description, series_description
        ),
        body_part_raw=body_raw,
        series_description=series_description,
        series_number=int(series_number) if series_number is not None else None,
        study_description=study_description,
        study_date=da.isoformat() if da else None,
        study_year=da.year if da else None,
        patient_sex=normalize_sex(first(ds, "PatientSex")),
        patient_age=derive_age(ds, study_dt),
        manufacturer=first(ds, "Manufacturer"),
        manufacturer_model=first(ds, "ManufacturerModelName"),
        institution=institution,
        sop_class_uid=first(ds, "SOPClassUID"),
        slice_thickness=number(ds, "SliceThickness"),
        magnetic_field_strength=number(ds, "MagneticFieldStrength"),
        project=ctx.get("project"),
        xnat_experiment_id=ctx.get("experiment_id"),
        xnat_session_label=ctx.get("session_label"),
        xnat_scan_id=ctx.get("scan_id"),
        xnat_subject_id=ctx.get("subject_id"),
        instance_count=max(int(instance_count), 1),
        deidentified=deidentified,
        source=str(ctx.get("source", "folder")),
    )


def series_doc_id(series_uid: str) -> str:
    """Deterministic OpenSearch ``_id`` for a series (the UID itself, validated)."""
    if not UID_RE.match(series_uid) or len(series_uid) > 64:
        raise ExtractionError("invalid SeriesInstanceUID")
    return series_uid


def study_doc_id(study_uid: str) -> str:
    if not UID_RE.match(study_uid) or len(study_uid) > 64:
        raise ExtractionError("invalid StudyInstanceUID")
    return study_uid
