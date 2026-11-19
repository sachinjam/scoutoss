from __future__ import annotations

from collections.abc import Callable
from io import BytesIO

import pydicom
import pytest
from pydicom.dataset import Dataset

from scoutoss_indexer.deid.deidentify import (
    METHOD_VERSION,
    DeidConfig,
    Deidentifier,
    DeidRejected,
    is_scoutoss_deidentified,
)
from scoutoss_indexer.deid.pseudonym import pseudonymize

PHI_STRINGS = [
    "Doe",
    "John",
    "MRN100001",
    "19500314",
    "Springfield",
    "742 Evergreen",
    "555-867",
    "123-45-6789",
    "House",
    "Quinn",
    "Tech",
    "CT-ROOM-3",
    "SN-998877",
    "ACC",
]


def _deid(salt: bytes, offset: int = -100) -> Deidentifier:
    return Deidentifier(DeidConfig(salt=salt, project="SCOUT_DEMO", offset_days=offset))


def _all_text(ds: Dataset) -> str:
    buf = BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue().decode("latin-1")


def test_removes_phi_everywhere(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    ds = make_ds()
    result = _deid(salt).apply(ds)
    blob = _all_text(ds)
    for phi in PHI_STRINGS:
        assert phi not in blob, phi
    assert ds.PatientID == result.pseudonym == pseudonymize("MRN100001", salt)
    assert str(ds.PatientName) == result.pseudonym
    assert ds.PatientBirthDate == ""
    assert ds.PatientAge == "071Y"  # derived before the birth date was blanked
    assert ds.PatientSex == "M"
    assert ds.PatientIdentityRemoved == "YES"
    assert METHOD_VERSION in ds.DeidentificationMethod
    assert {i.CodeValue for i in ds.DeidentificationMethodCodeSequence} >= {"113100", "113107"}
    assert ds.InstitutionName.startswith("SITE-")
    assert "PatientAddress" not in ds and "OtherPatientIDs" not in ds
    assert not any(e.tag.is_private for e in ds.iterall())
    assert ds.file_meta.MediaStorageSOPInstanceUID == ds.SOPInstanceUID
    assert is_scoutoss_deidentified(ds)
    assert (
        ds.PatientComments
        == f"Project: SCOUT_DEMO Subject: {result.pseudonym} Session: {result.session_label}"
    )
    assert result.original_patient_id == "MRN100001"


def test_dates_shifted_and_uids_remapped_deterministically(
    make_ds: Callable[..., Dataset], salt: bytes
) -> None:
    a, b = make_ds(), make_ds()
    orig_uid = b.StudyInstanceUID = a.StudyInstanceUID
    _deid(salt, -100).apply(a)
    _deid(salt, -100).apply(b)
    assert a.StudyDate == "20211002"  # 2022-01-10 - 100 days
    assert a.StudyTime == "093000"  # times kept
    assert a.StudyInstanceUID == b.StudyInstanceUID != orig_uid
    assert a.StudyInstanceUID.startswith("2.25.")
    assert a.SOPClassUID == "1.2.840.10008.5.1.4.1.1.2"  # standard UIDs untouched
    assert a.SeriesDescription == "Axial Lung"  # cleaned descriptor kept
    assert "[NAME]" in a.ImageComments


def test_relative_order_across_patients(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    early, late = make_ds(StudyDate="20200101"), make_ds(StudyDate="20210601", PatientID="MRN2")
    d = _deid(salt, -300)
    d.apply(early)
    d.apply(late)
    assert early.StudyDate < late.StudyDate


def test_age_capping_and_existing_age(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    old = make_ds(PatientAge="095Y")
    _deid(salt).apply(old)
    assert old.PatientAge == "090Y"
    young = make_ds(PatientAge="045Y")
    _deid(salt).apply(young)
    assert young.PatientAge == "045Y"
    nobirth = make_ds(PatientBirthDate=None)
    _deid(salt).apply(nobirth)
    assert "PatientAge" not in nobirth


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"BurnedInAnnotation": "YES"}, "burned-in"),
        ({"Modality": "SR"}, "free-text"),
        ({"PatientID": None}, "PatientID"),
        ({"StudyInstanceUID": None}, "StudyInstanceUID"),
    ],
)
def test_rejections(
    make_ds: Callable[..., Dataset], salt: bytes, overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(DeidRejected, match=reason):
        _deid(salt).apply(make_ds(**overrides))


def test_encapsulated_document_rejected(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    ds = make_ds()
    ds.EncapsulatedDocument = b"%PDF-1.4"
    with pytest.raises(DeidRejected, match="encapsulated"):
        _deid(salt).apply(ds)


def test_sequences_are_walked(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    ds = make_ds()
    item = Dataset()
    item.ReferencedSOPInstanceUID = "1.2.3.4.5"
    item.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    item.PersonName = "Doe^John"
    item.add_new(0x00191001, "LO", "secret")
    nested = Dataset()
    nested.ContentDate = "20220110"
    nested.ObservationDateTime = "20220110120000"
    nested.VerifyingObserverName = "Doe^Jane"
    item.ContentSequence = pydicom.Sequence([nested])
    ds.ReferencedImageSequence = pydicom.Sequence([item])
    ds.ReferringPhysicianIdentificationSequence = pydicom.Sequence([Dataset()])
    ds.OtherPatientIDsSequence = pydicom.Sequence([Dataset()])
    ds.AccessionNumber = "ACC1"
    ds.add_new(0x60003000, "OB", b"\x00" * 8)
    _deid(salt, -10).apply(ds)
    ref = ds.ReferencedImageSequence[0]
    assert ref.ReferencedSOPInstanceUID.startswith("2.25.")
    assert ref.ReferencedSOPClassUID == "1.2.840.10008.5.1.4.1.1.2"
    assert "PersonName" not in ref
    assert not any(e.tag.is_private for e in ref)
    inner = ref.ContentSequence[0]
    assert inner.ContentDate == "20211231"
    assert inner.ObservationDateTime == "20211231120000"
    assert inner.VerifyingObserverName == ""
    assert "OtherPatientIDsSequence" not in ds
    assert ds.AccessionNumber == ""
    assert (0x6000, 0x3000) not in ds


def test_multivalue_and_generic_pn(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    ds = make_ds()
    ds.ConsultingPhysicianName = ["Doe^A", "Roe^B"]  # PN not in the table -> emptied
    ds.add_new(0x00081110, "SQ", pydicom.Sequence())
    ds.SeriesDescription = ["CT Chest", "John Smith"]
    ds.AcquisitionDateTime = ["20220110120000", "20220111120000"]
    _deid(salt, -1).apply(ds)
    assert ds.ConsultingPhysicianName == ""
    assert list(ds.SeriesDescription)[1] == "[NAME]"
    assert list(ds.AcquisitionDateTime) == ["20220109120000", "20220110120000"]


def test_institution_missing(make_ds: Callable[..., Dataset], salt: bytes) -> None:
    ds = make_ds(InstitutionName="")
    _deid(salt).apply(ds)
    assert "InstitutionName" not in ds


def test_positive_offset_refused(salt: bytes) -> None:
    with pytest.raises(ValueError, match="past"):
        Deidentifier(DeidConfig(salt=salt, project="P", offset_days=5))


def test_method_description_fits_lo_vr() -> None:
    from scoutoss_indexer.deid.deidentify import METHOD_DESCRIPTION

    assert len(METHOD_DESCRIPTION) <= 64 and METHOD_VERSION in METHOD_DESCRIPTION
