"""Apply the ScoutOSS de-identification profile to a pydicom Dataset in place."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.sequence import Sequence
from pydicom.tag import BaseTag

from scoutoss_common.phi import redact_text
from scoutoss_indexer.deid.dates import dicom_age, parse_da, parse_dicom_age, shift_da, shift_dt
from scoutoss_indexer.deid.profile import ACTIONS, METHOD_CODES, REJECTED_MODALITIES, Action
from scoutoss_indexer.deid.pseudonym import hash_uid, pseudonymize, short_hash, site_code

METHOD_VERSION = "ScoutOSS-DEID-1"
# DeidentificationMethod is LO (max 64 chars); the full profile is in the code sequence.
METHOD_DESCRIPTION = f"{METHOD_VERSION} PS3.15 Basic+RetLongModifDates+RetPatChars+CleanDesc"[:64]


class DeidRejected(Exception):
    """The object cannot be safely de-identified and must not be forwarded."""


@dataclass(frozen=True)
class DeidConfig:
    salt: bytes
    project: str
    offset_days: int


@dataclass(frozen=True)
class DeidResult:
    pseudonym: str
    original_patient_id: str  # needed for the encrypted vault; NEVER log this
    study_uid: str
    series_uid: str
    sop_uid: str
    session_label: str


def is_scoutoss_deidentified(ds: Dataset) -> bool:
    return str(ds.get("PatientIdentityRemoved", "")).upper() == "YES" and METHOD_VERSION in str(
        ds.get("DeidentificationMethod", "")
    )


class Deidentifier:
    def __init__(self, config: DeidConfig) -> None:
        if config.offset_days > 0:
            raise ValueError("date offsets must move dates into the past")
        self.config = config

    # ------------------------------------------------------------------ public
    def apply(self, ds: Dataset) -> DeidResult:
        self._precheck(ds)
        patient_id = str(ds.get("PatientID", "") or "").strip()
        if not patient_id:
            raise DeidRejected("missing PatientID")
        orig_study_uid = str(ds.get("StudyInstanceUID", "") or "")
        if not orig_study_uid:
            raise DeidRejected("missing StudyInstanceUID")

        pseudonym = pseudonymize(patient_id, self.config.salt)
        self._set_age(ds)
        institution = site_code(_text(ds.get("InstitutionName")), self.config.salt)
        modality = str(ds.get("Modality", "OT") or "OT").upper()

        self._walk(ds)
        ds.remove_private_tags()

        ds.PatientID = pseudonym
        ds.PatientName = pseudonym
        if institution:
            ds.InstitutionName = institution
        elif "InstitutionName" in ds:
            del ds.InstitutionName
        label = f"{pseudonym}_{modality}_{short_hash(orig_study_uid, self.config.salt)}"
        ds.PatientComments = f"Project: {self.config.project} Subject: {pseudonym} Session: {label}"
        ds.PatientIdentityRemoved = "YES"
        ds.DeidentificationMethod = METHOD_DESCRIPTION
        ds.LongitudinalTemporalInformationModified = "MODIFIED"
        codes = Sequence()
        for value, meaning in METHOD_CODES:
            item = Dataset()
            item.CodeValue = value
            item.CodingSchemeDesignator = "DCM"
            item.CodeMeaning = meaning
            codes.append(item)
        ds.DeidentificationMethodCodeSequence = codes

        meta = getattr(ds, "file_meta", None)
        if meta is not None and "SOPInstanceUID" in ds:
            meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID

        return DeidResult(
            pseudonym=pseudonym,
            original_patient_id=patient_id,
            study_uid=str(ds.get("StudyInstanceUID", "")),
            series_uid=str(ds.get("SeriesInstanceUID", "")),
            sop_uid=str(ds.get("SOPInstanceUID", "")),
            session_label=label,
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _precheck(ds: Dataset) -> None:
        if str(ds.get("BurnedInAnnotation", "")).upper() == "YES":
            raise DeidRejected("burned-in annotation present")
        if str(ds.get("Modality", "")).upper() in REJECTED_MODALITIES:
            raise DeidRejected("modality carries free-text content")
        if "EncapsulatedDocument" in ds:
            raise DeidRejected("encapsulated document")

    def _set_age(self, ds: Dataset) -> None:
        existing = parse_dicom_age(_text(ds.get("PatientAge")))
        if existing is not None:
            if existing >= 90:
                ds.PatientAge = "090Y"
            return
        birth = parse_da(_text(ds.get("PatientBirthDate")))
        study = parse_da(_text(ds.get("StudyDate")) or _text(ds.get("SeriesDate")))
        if birth and study and study >= birth:
            ds.PatientAge = dicom_age(birth, study)

    def _walk(self, ds: Dataset) -> None:
        for tag in list(ds.keys()):
            elem = ds[tag]
            group = BaseTag(tag).group
            if BaseTag(tag).is_private or 0x5000 <= group <= 0x50FF or 0x6000 <= group <= 0x60FF:
                del ds[tag]
                continue
            keyword = elem.keyword
            action = ACTIONS.get(keyword)
            if action is Action.REMOVE:
                del ds[tag]
            elif action is Action.ZERO:
                elem.value = "" if elem.VR != "SQ" else Sequence()
            elif action is Action.CLEAN:
                elem.value = _clean(elem.value)
            elif action in (Action.KEEP, Action.DUMMY):
                continue  # dummies are written explicitly in apply()
            elif elem.VR == "SQ":
                for item in elem.value:
                    self._walk(item)
            elif elem.VR == "UI":
                elem.value = _map(elem.value, lambda v: hash_uid(v, self.config.salt))
            elif elem.VR == "DA":
                elem.value = _map(elem.value, lambda v: shift_da(v, self.config.offset_days) or "")
            elif elem.VR == "DT":
                elem.value = _map(elem.value, lambda v: shift_dt(v, self.config.offset_days) or "")
            elif elem.VR == "PN":
                elem.value = ""


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, MultiValue):
        value = value[0] if len(value) else None
        if value is None:
            return None
    text = str(value).strip()
    return text or None


def _map(value: object, func: Callable[[str], str]) -> object:
    if value is None or value == "":
        return value
    if isinstance(value, MultiValue | list):
        return [func(str(v)) for v in value]
    return func(str(value))


def _clean(value: object) -> object:
    if value is None:
        return value
    if isinstance(value, MultiValue | list):
        return [redact_text(str(v)) for v in value]
    return redact_text(str(value))
