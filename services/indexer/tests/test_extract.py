from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue

from scoutoss_indexer.deid.deidentify import DeidConfig, Deidentifier
from scoutoss_indexer.deid.pseudonym import pseudonymize
from scoutoss_indexer.extract import (
    ExtractionError,
    extract_series,
    normalize_body_part,
    normalize_sex,
    read_header,
    series_doc_id,
    study_doc_id,
    text,
)


def _bytes(ds: Dataset) -> bytes:
    buf = BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def test_extracts_core_fields(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    ds = make_ds()
    rec = extract_series(
        read_header(_bytes(ds)),
        salt=salt,
        instance_count=12,
        context={
            "project": "P1",
            "experiment_id": "XNAT_E1",
            "scan_id": "1",
            "session_label": "S1",
            "subject_id": "XNAT_S1",
            "source": "xnat",
        },
    )
    assert rec.modality == "CT"
    assert rec.body_part == "CHEST"
    assert rec.study_date == "2022-01-10" and rec.study_year == 2022
    assert rec.patient_sex == "M"
    assert rec.patient_age == pytest.approx(71.8, abs=0.1)  # from birth date
    assert rec.instance_count == 12
    assert (
        rec.project == "P1"
        and rec.xnat_experiment_id == "XNAT_E1"
        and rec.xnat_subject_id == "XNAT_S1"
    )
    assert rec.manufacturer == "SIEMENS"
    assert rec.institution is not None and rec.institution.startswith("SITE-")
    # raw IDs never reach the record
    assert rec.patient_pseudonym == pseudonymize("MRN100001", salt)
    assert "MRN" not in str(rec.to_doc()) and "Springfield" not in str(rec.to_doc())


def test_deidentified_input_is_trusted(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    ds = make_ds()
    result = Deidentifier(DeidConfig(salt=salt, project="P", offset_days=-30)).apply(ds)
    rec = extract_series(ds, salt=salt)
    assert rec.patient_pseudonym == result.pseudonym
    assert rec.deidentified is True
    assert rec.institution == ds.InstitutionName
    assert rec.patient_age == 71.0  # from the PatientAge the gateway wrote
    assert rec.study_date == "2021-12-11"


def test_missing_tags(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    ds = make_ds(
        BodyPartExamined=None,
        PatientSex=None,
        Manufacturer=None,
        StudyDate=None,
        SeriesDate=None,
        ContentDate=None,
        PatientBirthDate=None,
        InstitutionName=None,
        SeriesNumber=None,
        PatientID=None,
        StudyDescription=None,
        SeriesDescription=None,
        SliceThickness=None,
    )
    rec = extract_series(ds, salt=salt)
    assert rec.body_part is None and rec.patient_sex is None and rec.manufacturer is None
    assert rec.study_date is None and rec.patient_age is None and rec.institution is None
    assert rec.series_number is None and rec.slice_thickness is None
    assert rec.patient_pseudonym.startswith("SP")  # synthesized from the study UID


def test_body_part_inferred_from_description(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    ds = make_ds(
        BodyPartExamined=None, StudyDescription="CT THORAX HIGH RES", SeriesDescription="axial"
    )
    assert extract_series(ds, salt=salt).body_part == "CHEST"


def test_multi_value_and_non_ascii(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    ds = make_ds(SpecificCharacterSet="ISO_IR 192")
    ds.StudyDescription = "IRM cérébrale — Größe"
    ds.BodyPartExamined = MultiValue(str, ["CHEST", "ABDOMEN"])
    ds.Manufacturer = MultiValue(str, ["SIEMENS", "ALT"])
    rec = extract_series(read_header(_bytes(ds)), salt=salt)
    assert rec.study_description == "IRM cérébrale — Größe"
    assert rec.body_part == "CHEST"
    assert rec.body_part_raw == "CHEST\\ABDOMEN"
    assert rec.manufacturer == "SIEMENS"


def test_malformed_values_become_none(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    from pydicom.dataelem import RawDataElement
    from pydicom.tag import Tag

    ds = make_ds(StudyDate="2022-13-45", PatientAge="old", PatientSex="unknown")
    del ds.SliceThickness
    ds[0x00180050] = RawDataElement(Tag(0x00180050), "DS", 5, b"thick", 0, False, True)
    rec = extract_series(ds, salt=salt)
    assert rec.study_date is None
    assert rec.patient_sex == "U"
    assert rec.slice_thickness is None


@pytest.mark.parametrize("field", ["StudyInstanceUID", "SeriesInstanceUID"])
def test_missing_identity_raises(make_ds: Callable[..., Dataset], salt: bytes, field: str) -> None:
    with pytest.raises(ExtractionError, match=field):
        extract_series(make_ds(**{field: None}), salt=salt)
    with pytest.raises(ExtractionError):
        extract_series(make_ds(**{field: "not a uid!"}), salt=salt)


def test_read_header_errors(tmp_path: Path) -> None:
    with pytest.raises(ExtractionError, match="not a DICOM"):
        read_header(b"hello world" * 20)
    bad = tmp_path / "bad.dcm"
    bad.write_bytes(b"\x00" * 128 + b"DICM" + b"\x02\x00\x10\x00UI\x40\x00" + b"1.2")
    with pytest.raises(ExtractionError):  # truncated header parses to nothing usable
        extract_series(read_header(bad), salt=b"s")


def test_doc_ids_deterministic() -> None:
    uid = "1.2.826.0.1.3680043.10.1234.5"
    assert series_doc_id(uid) == series_doc_id(uid) == uid
    assert study_doc_id(uid) == uid
    with pytest.raises(ExtractionError):
        series_doc_id("1.2.x")
    with pytest.raises(ExtractionError):
        study_doc_id("9" * 70)


def test_helpers(make_ds: Callable[..., Dataset]) -> None:
    ds = make_ds()
    assert text(ds, "NotAKeyword") is None
    ds.ImageType = ["ORIGINAL", "", "AXIAL"]
    assert text(ds, "ImageType") == "ORIGINAL\\AXIAL"
    assert normalize_body_part("thorax") == "CHEST"
    assert normalize_body_part("L-SPINE") == "LSPINE"
    assert normalize_body_part(None, "MR brain") == "HEAD"
    assert normalize_body_part(None, None) is None
    assert normalize_body_part("---", "pelvis") == "PELVIS"
    assert normalize_sex("female") == "F" and normalize_sex(None) is None
