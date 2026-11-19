"""PHI detection and redaction.

Used in three places, all with the same rules:

* the log processor (nothing that looks like PHI reaches stdout / Filebeat),
* the copilot's outbound-prompt middleware (nothing that looks like PHI reaches an LLM),
* the de-identification "Clean Descriptors" option (study/series descriptions).

The rules are deliberately conservative: false positives cost a little context, false
negatives leak PHI. DICOM UIDs (digits and dots) are never touched so logs stay useful.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Keys whose *values* are always removed from structured data (logs, audit params).
PHI_KEYS: frozenset[str] = frozenset(
    {
        "patient_name",
        "patientname",
        "patient_id",
        "patientid",
        "other_patient_ids",
        "birth_date",
        "birthdate",
        "patient_birth_date",
        "patientbirthdate",
        "dob",
        "mrn",
        "ssn",
        "address",
        "patient_address",
        "phone",
        "telephone",
        "email",
        "accession_number",
        "referring_physician",
        "dataset",
        "dicom",
        "payload",
        "pixel_data",
        "raw",
        "hl7",
        "hl7_message",
        "message_body",
    }
)

# Medical / domain vocabulary that looks like "Firstname Lastname" but is not a name.
_DOMAIN_TERMS: frozenset[str] = frozenset(
    t.lower()
    for t in (
        "Lung Cancer",
        "Breast Cancer",
        "Computed Tomography",
        "Magnetic Resonance",
        "Positron Emission",
        "Body Part",
        "Study Date",
        "Series Description",
        "Study Description",
        "Review Code",
        "Save Cohort",
        "Run Pipeline",
        "Head Neck",
        "Lung Nodule",
        "Prostate Cancer",
        "Colon Cancer",
        "Brain Tumor",
        "Chest Pain",
        "Total Segmentator",
        "Open Search",
        "Siemens Healthineers",
        "Philips Healthcare",
        "Canon Medical",
        "General Electric",
        "Ge Healthcare",
        "Fuji Film",
        "Show Me",
        "How Many",
        "What Is",
        "List All",
        "Count The",
        "Break Down",
        "Group By",
    )
)

# Single words that make a capitalised pair "clearly not a person" (anatomy, modality,
# procedure and query vocabulary). Lower-case.
_DOMAIN_WORDS: frozenset[str] = frozenset(
    [
        "abdomen",
        "abdominal",
        "adrenal",
        "angio",
        "angiography",
        "ankle",
        "aorta",
        "arm",
        "axial",
        "bladder",
        "body",
        "bone",
        "brain",
        "breast",
        "cancer",
        "cardiac",
        "carcinoma",
        "cervical",
        "chest",
        "colon",
        "contrast",
        "coronal",
        "coronary",
        "cranial",
        "dental",
        "diffusion",
        "elbow",
        "extremity",
        "femur",
        "foot",
        "hand",
        "head",
        "heart",
        "hip",
        "kidney",
        "knee",
        "leg",
        "liver",
        "lumbar",
        "lung",
        "lungs",
        "lymphoma",
        "mammo",
        "mammography",
        "neck",
        "nodule",
        "pancreas",
        "pelvis",
        "perfusion",
        "prostate",
        "renal",
        "routine",
        "sagittal",
        "shoulder",
        "sinus",
        "skull",
        "spine",
        "spleen",
        "stroke",
        "thoracic",
        "thorax",
        "thyroid",
        "tumor",
        "tumour",
        "wrist",
        "whole",
        "tomography",
        "resonance",
        "imaging",
        "scan",
        "series",
        "study",
        "studies",
        "patients",
        "cohort",
        "cohorts",
        "count",
        "total",
        "average",
        "median",
        "mean",
        "male",
        "female",
        "modality",
        "modalities",
        "sex",
        "age",
        "ages",
        "manufacturer",
        "institution",
        "site",
        "year",
        "years",
        "with",
        "without",
        "and",
        "or",
        "plus",
        "pet",
        "spect",
        "mri",
        "ultrasound",
        "xray",
        "x-ray",
        "radiograph",
        "nuclear",
        "medicine",
        "oncology",
        "protocol",
        "localizer",
        "scout",
        "recon",
        "reformat",
        "bolus",
        "delayed",
        "arterial",
        "venous",
        "portal",
        "phase",
        "pre",
        "post",
        "enhanced",
        "unenhanced",
        "high",
        "resolution",
        "low",
        "dose",
        "screening",
        "follow",
        "followup",
        "baseline",
        "histogram",
        "chart",
        "bar",
        "pie",
        "donut",
        "distribution",
        "breakdown",
        "show",
        "list",
        "find",
        "give",
        "plot",
        "graph",
        "compare",
        "between",
        "under",
        "over",
        "above",
        "below",
        "older",
        "younger",
        "healthineers",
        "healthcare",
        "medical",
        "systems",
        "siemens",
        "philips",
        "canon",
        "toshiba",
        "hitachi",
        "fujifilm",
        "general",
        "electric",
        "hologic",
        "agfa",
        "carestream",
        "samsung",
    ]
)

_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<![\d.])(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?![\d.])")
_MRN = re.compile(r"(?i)\b(?:mrn|medical record(?: number)?|patient ?id)\s*[:#=]?\s*[A-Z0-9-]{3,}")
_LONG_DIGITS = re.compile(r"(?<![\d.\w])\d{7,}(?![\d.\w])")
_ISO_DATE = re.compile(r"\b(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b")
_US_DATE = re.compile(r"\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/(?:19|20)?\d{2}\b")
_DICOM_PN = re.compile(r"\b[A-Za-z][A-Za-z'\-]*\^[A-Za-z][A-Za-z'\-]*(?:\^[A-Za-z'\-]*)*")
_HONORIFIC = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr|Prof)\.?\s+[A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]+)?")
_LABELLED_NAME = re.compile(
    r"(?i:\b(?:patient|pt|name|named|called|mr\.?|mrs\.?|ms\.?)\s*[:=]?\s+)"
    r"([A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]+){0,2})"
)
_CAP_PAIR = re.compile(r"\b([A-Z][a-z'\-]{1,})\s+([A-Z][a-z'\-]{1,})(?:\s+([A-Z][a-z'\-]{1,}))?\b")
_STREET = re.compile(
    r"(?i)\b\d{1,5}\s+(?:[a-z0-9]+\s){1,3}"
    r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|way)\b\.?"
)
_ZIP_STATE = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")


def redact_text(
    text: str,
    *,
    allow_terms: Iterable[str] = (),
    redact_dates: bool = True,
    detect_names: bool = True,
) -> str:
    """Return ``text`` with PHI-like spans replaced by typed placeholders.

    ``allow_terms`` are case-insensitive phrases that must survive (e.g. manufacturer
    names that appear as de-identified values in the index).
    """
    if not text:
        return text
    allowed = {t.lower() for t in allow_terms} | _DOMAIN_TERMS
    out = _SSN.sub("[SSN]", text)
    out = _EMAIL.sub("[EMAIL]", out)
    out = _MRN.sub("[MRN]", out)
    out = _PHONE.sub("[PHONE]", out)
    out = _STREET.sub("[ADDRESS]", out)
    out = _ZIP_STATE.sub("[ADDRESS]", out)
    out = _DICOM_PN.sub("[NAME]", out)
    if redact_dates:
        out = _ISO_DATE.sub("[DATE]", out)
        out = _US_DATE.sub("[DATE]", out)
    out = _LONG_DIGITS.sub("[ID]", out)
    if detect_names:
        out = _HONORIFIC.sub("[NAME]", out)

        def _labelled(m: re.Match[str]) -> str:
            name = m.group(1)
            if name.lower() in allowed or name.split()[0].lower() in _DOMAIN_WORDS:
                return m.group(0)
            return m.group(0).replace(name, "[NAME]")

        out = _LABELLED_NAME.sub(_labelled, out)

        def _pair(m: re.Match[str]) -> str:
            phrase = m.group(0)
            two = f"{m.group(1)} {m.group(2)}".lower()
            if phrase.lower() in allowed or two in allowed:
                return phrase
            words = [w.lower() for w in phrase.split()]
            if any(w in allowed or w in _DOMAIN_WORDS for w in words):
                return phrase
            return "[NAME]"

        out = _CAP_PAIR.sub(_pair, out)
    return out


def contains_phi(text: str, *, allow_terms: Iterable[str] = ()) -> bool:
    """True if :func:`redact_text` would change ``text``."""
    return redact_text(text, allow_terms=allow_terms) != text


def scrub_mapping(data: Mapping[str, Any], *, depth: int = 0) -> dict[str, Any]:
    """Recursively drop PHI keys and redact PHI-looking string values."""
    clean: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in PHI_KEYS:
            clean[key] = REDACTED
        else:
            clean[key] = scrub_value(value, depth=depth + 1)
    return clean


def scrub_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value, detect_names=False)
    if isinstance(value, Mapping):
        return scrub_mapping(value, depth=depth)
    if isinstance(value, list | tuple | set | frozenset):
        return [scrub_value(v, depth=depth + 1) for v in value]
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(value)} bytes>"
    return value
