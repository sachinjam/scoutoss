"""OpenSearch index definitions and the queryable field catalog, shared by the indexer
(which creates the indices) and the copilot (which prompts with and validates against it)."""

from __future__ import annotations

from typing import Any

SERIES_INDEX = "dicom_series"
STUDIES_INDEX = "dicom_studies"
PATIENTS_INDEX = "dicom_patients"
DLQ_INDEX = "indexer_dlq"
CLINICAL_INDEX = "clinical_events"

_SETTINGS: dict[str, Any] = {
    "number_of_shards": 1,
    "number_of_replicas": 0,
    "analysis": {
        "normalizer": {"upper": {"type": "custom", "filter": ["uppercase", "asciifolding"]}},
    },
}

_KW_UPPER = {"type": "keyword", "normalizer": "upper"}
_TEXT_KW = {"type": "text", "fields": {"raw": {"type": "keyword", "ignore_above": 256}}}

SERIES_MAPPING: dict[str, Any] = {
    "settings": _SETTINGS,
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "series_uid": {"type": "keyword"},
            "study_uid": {"type": "keyword"},
            "patient_pseudonym": {"type": "keyword"},
            "modality": _KW_UPPER,
            "body_part": _KW_UPPER,
            "body_part_raw": {"type": "keyword"},
            "series_description": _TEXT_KW,
            "series_number": {"type": "integer"},
            "study_description": _TEXT_KW,
            "study_date": {"type": "date", "format": "strict_date"},
            "study_year": {"type": "integer"},
            "patient_sex": {"type": "keyword"},
            "patient_age": {"type": "float"},
            "manufacturer": {"type": "keyword"},
            "manufacturer_model": {"type": "keyword"},
            "institution": {"type": "keyword"},
            "sop_class_uid": {"type": "keyword"},
            "slice_thickness": {"type": "float"},
            "magnetic_field_strength": {"type": "float"},
            "project": {"type": "keyword"},
            "xnat_experiment_id": {"type": "keyword"},
            "xnat_session_label": {"type": "keyword"},
            "xnat_scan_id": {"type": "keyword"},
            "xnat_subject_id": {"type": "keyword"},
            "instance_count": {"type": "integer"},
            "deidentified": {"type": "boolean"},
            "source": {"type": "keyword"},
            "indexed_at": {"type": "date"},
        },
    },
}

STUDIES_MAPPING: dict[str, Any] = {
    "settings": _SETTINGS,
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "study_uid": {"type": "keyword"},
            "patient_pseudonym": {"type": "keyword"},
            "project": {"type": "keyword"},
            "study_date": {"type": "date", "format": "strict_date"},
            "study_year": {"type": "integer"},
            "study_description": _TEXT_KW,
            "modalities": _KW_UPPER,
            "primary_modality": _KW_UPPER,
            "body_parts": _KW_UPPER,
            "series_count": {"type": "integer"},
            "instance_count": {"type": "integer"},
            "patient_sex": {"type": "keyword"},
            "patient_age": {"type": "float"},
            "manufacturers": {"type": "keyword"},
            "institution": {"type": "keyword"},
            "xnat_experiment_id": {"type": "keyword"},
            "xnat_session_label": {"type": "keyword"},
            "xnat_subject_id": {"type": "keyword"},
            "viewer_url": {"type": "keyword", "index": False},
            "indexed_at": {"type": "date"},
        },
    },
}

PATIENTS_MAPPING: dict[str, Any] = {
    "settings": _SETTINGS,
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "patient_pseudonym": {"type": "keyword"},
            "patient_sex": {"type": "keyword"},
            "age_latest": {"type": "float"},
            "study_count": {"type": "integer"},
            "series_count": {"type": "integer"},
            "modalities": _KW_UPPER,
            "body_parts": _KW_UPPER,
            "projects": {"type": "keyword"},
            "first_study_date": {"type": "date", "format": "strict_date"},
            "last_study_date": {"type": "date", "format": "strict_date"},
            "diagnoses": {"type": "keyword"},
            "diagnosis_codes": {"type": "keyword"},
            "indexed_at": {"type": "date"},
        },
    },
}

DLQ_MAPPING: dict[str, Any] = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "ref": {"type": "keyword"},
            "source": {"type": "keyword"},
            "stage": {"type": "keyword"},
            "error_type": {"type": "keyword"},
            "error": {"type": "text"},
            "attempts": {"type": "integer"},
            "ts": {"type": "date"},
        },
    },
}

CLINICAL_MAPPING: dict[str, Any] = {
    "settings": _SETTINGS,
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "event_id": {"type": "keyword"},
            "patient_pseudonym": {"type": "keyword"},
            "message_type": {"type": "keyword"},
            "event_type": {"type": "keyword"},
            "event_date": {"type": "date", "format": "strict_date"},
            "patient_sex": {"type": "keyword"},
            "patient_age": {"type": "float"},
            "diagnosis_code": {"type": "keyword"},
            "diagnosis_system": {"type": "keyword"},
            "diagnosis_text": _TEXT_KW,
            "diagnosis_category": {"type": "keyword"},
            "order_code": {"type": "keyword"},
            "order_text": _TEXT_KW,
            "lab_code": {"type": "keyword"},
            "lab_name": _TEXT_KW,
            "lab_value": {"type": "float"},
            "lab_value_text": {"type": "keyword"},
            "lab_units": {"type": "keyword"},
            "lab_flag": {"type": "keyword"},
            "source": {"type": "keyword"},
            "indexed_at": {"type": "date"},
        },
    },
}

INDEX_DEFINITIONS: dict[str, dict[str, Any]] = {
    SERIES_INDEX: SERIES_MAPPING,
    STUDIES_INDEX: STUDIES_MAPPING,
    PATIENTS_INDEX: PATIENTS_MAPPING,
    DLQ_INDEX: DLQ_MAPPING,
    CLINICAL_INDEX: CLINICAL_MAPPING,
}

# Human descriptions for the copilot prompt. Only these indices/fields are queryable.
FIELD_DESCRIPTIONS: dict[str, dict[str, str]] = {
    STUDIES_INDEX: {
        "study_uid": "de-identified StudyInstanceUID",
        "patient_pseudonym": "patient pseudonym (join key across indices)",
        "project": "XNAT project id",
        "study_date": "study date (date-shifted per project), yyyy-mm-dd",
        "study_year": "year of (shifted) study date",
        "study_description": "cleaned study description (text; .raw keyword)",
        "modalities": "modalities in the study, e.g. CT, MR, PT, CR",
        "primary_modality": "first/primary modality",
        "body_parts": "normalized body parts, e.g. CHEST, HEAD, ABDOMEN",
        "series_count": "number of series",
        "instance_count": "number of images",
        "patient_sex": "M, F, O or U",
        "patient_age": "age at study in years (ages >89 reported as 90)",
        "manufacturers": "scanner manufacturers",
        "institution": "coarse site code (SITE-xxxxxx)",
        "xnat_experiment_id": "XNAT session (experiment) id",
    },
    SERIES_INDEX: {
        "series_uid": "de-identified SeriesInstanceUID",
        "study_uid": "parent study",
        "patient_pseudonym": "patient pseudonym",
        "modality": "series modality",
        "body_part": "normalized body part",
        "series_description": "cleaned series description",
        "study_date": "shifted study date",
        "patient_sex": "M, F, O or U",
        "patient_age": "age at study in years",
        "manufacturer": "scanner manufacturer",
        "manufacturer_model": "scanner model",
        "slice_thickness": "slice thickness in mm",
        "magnetic_field_strength": "MR field strength in tesla",
        "instance_count": "images in the series",
        "project": "XNAT project id",
    },
    PATIENTS_INDEX: {
        "patient_pseudonym": "patient pseudonym",
        "patient_sex": "M, F, O or U",
        "age_latest": "age at most recent study",
        "study_count": "number of imaging studies",
        "modalities": "all modalities imaged",
        "body_parts": "all body parts imaged",
        "projects": "XNAT projects",
        "first_study_date": "first (shifted) study date",
        "last_study_date": "last (shifted) study date",
        "diagnoses": "diagnosis categories from HL7 feeds, e.g. LUNG_CANCER",
        "diagnosis_codes": "ICD-10 codes from HL7 feeds",
    },
    CLINICAL_INDEX: {
        "patient_pseudonym": "patient pseudonym (joins to imaging)",
        "message_type": "HL7 message type, e.g. ADT^A01, ORU^R01",
        "event_type": "admission, update, order, result, diagnosis",
        "event_date": "shifted event date",
        "patient_sex": "M, F, O or U",
        "patient_age": "age at event in years",
        "diagnosis_code": "ICD-10 code, e.g. C34.90",
        "diagnosis_text": "diagnosis description",
        "diagnosis_category": "normalized category, e.g. LUNG_CANCER, BREAST_CANCER",
        "order_code": "order code",
        "order_text": "order description",
        "lab_code": "LOINC or local lab code",
        "lab_name": "lab test name",
        "lab_value": "numeric lab value",
        "lab_units": "lab units",
        "lab_flag": "abnormal flag (H, L, N, A)",
    },
}

# Low-cardinality keyword fields whose (de-identified) top values may be shown to the LLM.
EXAMPLE_VALUE_FIELDS: dict[str, tuple[str, ...]] = {
    STUDIES_INDEX: ("modalities", "body_parts", "patient_sex", "manufacturers", "project"),
    CLINICAL_INDEX: ("diagnosis_category", "event_type", "message_type", "lab_code"),
}


def field_type(index: str, field: str) -> str | None:
    props = INDEX_DEFINITIONS.get(index, {}).get("mappings", {}).get("properties", {})
    base, _, sub = field.partition(".")
    spec = props.get(base)
    if spec is None:
        return None
    if sub:
        sub_spec = spec.get("fields", {}).get(sub)
        return str(sub_spec["type"]) if sub_spec else None
    return str(spec["type"])
