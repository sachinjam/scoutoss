"""Synthetic, *fake-PHI* DICOM studies for tests, seeding and demos.

Every patient name, MRN and date here is invented. The data deliberately contains
PHI-shaped values (names, MRNs, birth dates, institution, physicians, accession numbers,
private tags) so the de-identification path has something real to remove. Pixel data is
a simple anatomical phantom so OHIF has something to display.

Run ``python -m scoutoss_indexer.synthetic OUT_DIR`` to write the default 10-study cohort.
"""

from __future__ import annotations

import argparse
import math
import random
import struct
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import PYDICOM_IMPLEMENTATION_UID, UID, ExplicitVRLittleEndian, generate_uid

CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"
MR_IMAGE = "1.2.840.10008.5.1.4.1.1.4"
CR_IMAGE = "1.2.840.10008.5.1.4.1.1.1"
PT_IMAGE = "1.2.840.10008.5.1.4.1.1.128"
SOP_CLASS = {"CT": CT_IMAGE, "MR": MR_IMAGE, "CR": CR_IMAGE, "PT": PT_IMAGE}
UID_ROOT = "1.2.826.0.1.3680043.10.1234."  # fake research root


@dataclass(frozen=True)
class FakePatient:
    name: str
    mrn: str
    sex: str
    birth_date: date
    charset: str = "ISO_IR 100"


@dataclass
class SeriesSpec:
    modality: str
    description: str
    body_part: str | None
    slices: int = 8
    series_uid: str = field(default_factory=lambda: generate_uid(prefix=UID_ROOT))


@dataclass
class StudySpec:
    patient: FakePatient
    study_date: date
    description: str
    series: list[SeriesSpec]
    manufacturer: str = "SIEMENS"
    model: str = "SOMATOM Force"
    institution: str = "Springfield General Hospital"
    accession: str = "ACC0000"
    study_uid: str = field(default_factory=lambda: generate_uid(prefix=UID_ROOT))


PATIENTS: tuple[FakePatient, ...] = (
    FakePatient("Doe^John", "MRN100001", "M", date(1950, 3, 14)),
    FakePatient("Roe^Jane", "MRN100002", "F", date(1962, 7, 2)),
    FakePatient("Smith^Alice", "MRN100003", "F", date(1975, 11, 23)),
    FakePatient("Müller^Jürgen", "MRN100004", "M", date(1944, 1, 30), "ISO_IR 192"),
    FakePatient("Nguyen^Bao", "MRN100005", "M", date(1988, 5, 5)),
    FakePatient("Garcia^Maria", "MRN100006", "F", date(1931, 9, 9)),  # >89 -> capped at 90
    FakePatient("Okafor^Chidi", "MRN100007", "M", date(1958, 12, 1)),
)

LUNG_CANCER_MRNS = ("MRN100001", "MRN100004", "MRN100006")


def default_cohort(seed: int = 42) -> list[StudySpec]:
    """Ten studies across seven patients (CT chest, MR brain, CR chest, PET, ...)."""
    rng = random.Random(seed)
    p = PATIENTS
    base = date(2022, 1, 10)

    def d(days: int) -> date:
        return base + timedelta(days=days)

    studies = [
        StudySpec(
            p[0],
            d(0),
            "CT CHEST W/O CONTRAST",
            [
                SeriesSpec("CT", "Axial 1.0 Lung", "CHEST", 10),
                SeriesSpec("CT", "Axial 3.0 Soft", "CHEST", 6),
            ],
        ),
        StudySpec(p[0], d(95), "CT CHEST FOLLOW UP", [SeriesSpec("CT", "Axial Lung", "CHEST", 8)]),
        StudySpec(
            p[1],
            d(12),
            "MR BRAIN W WO CONTRAST",
            [SeriesSpec("MR", "T1 AX", "BRAIN", 6), SeriesSpec("MR", "T2 FLAIR", "BRAIN", 6)],
            manufacturer="GE MEDICAL SYSTEMS",
            model="SIGNA Premier",
        ),
        StudySpec(
            p[2],
            d(30),
            "CR CHEST PA AND LATERAL",
            [SeriesSpec("CR", "PA", "CHEST", 1)],
            manufacturer="FUJIFILM Corporation",
            model="FCR",
        ),
        StudySpec(
            p[3],
            d(44),
            "CT CHEST ABDOMEN PELVIS W CONTRAST",
            [SeriesSpec("CT", "Portal venous", "ABDOMEN", 8)],
        ),
        StudySpec(
            p[3],
            d(60),
            "PET CT WHOLE BODY",
            [SeriesSpec("PT", "PET AC", "WHOLEBODY", 6)],
            manufacturer="SIEMENS",
            model="Biograph Vision",
        ),
        StudySpec(
            p[4],
            d(71),
            "MR KNEE LEFT",
            [SeriesSpec("MR", "PD FS SAG", "KNEE", 6)],
            manufacturer="Philips",
            model="Ingenia",
        ),
        StudySpec(
            p[5],
            d(80),
            "CT CHEST LOW DOSE SCREENING",
            [SeriesSpec("CT", "Lung 1mm", None, 8)],
            manufacturer="Canon Medical Systems",
            model="Aquilion ONE",
        ),
        StudySpec(
            p[6],
            d(88),
            "CT HEAD WITHOUT CONTRAST",
            [SeriesSpec("CT", "Brain 5mm", "HEAD", 6)],
            manufacturer="GE MEDICAL SYSTEMS",
            model="Revolution CT",
        ),
        StudySpec(p[1], d(120), "CT ABDOMEN PELVIS", [SeriesSpec("CT", "Axial 5mm", "ABDOMEN", 6)]),
    ]
    for i, s in enumerate(studies):
        s.accession = f"ACC{20000 + i}"
        s.study_uid = generate_uid(prefix=UID_ROOT, entropy_srcs=[str(seed), "study", str(i)])
        for j, series in enumerate(s.series):
            series.series_uid = generate_uid(
                prefix=UID_ROOT, entropy_srcs=[str(seed), "series", str(i), str(j)]
            )
    rng.shuffle(studies)
    return studies


GIVEN_NAMES = {
    "M": [
        "James",
        "Wei",
        "Arjun",
        "Mateo",
        "Kwame",
        "Hiroshi",
        "Omar",
        "Lukas",
        "Tomasz",
        "Rafael",
        "Dmitri",
        "Samuel",
        "Ibrahim",
        "Noah",
        "Viktor",
        "Andre",
        "Tariq",
        "Elias",
        "Jonas",
        "Felix",
    ],
    "F": [
        "Mary",
        "Li",
        "Priya",
        "Sofia",
        "Amara",
        "Yuki",
        "Fatima",
        "Hannah",
        "Zofia",
        "Camila",
        "Olga",
        "Ruth",
        "Aisha",
        "Emma",
        "Ingrid",
        "Chloe",
        "Leila",
        "Clara",
        "Greta",
        "Nora",
    ],
}
FAMILY_NAMES = [
    "Anderson",
    "Chen",
    "Patel",
    "Rodriguez",
    "Mensah",
    "Tanaka",
    "Haddad",
    "Schmidt",
    "Kowalski",
    "Silva",
    "Ivanov",
    "Cohen",
    "Diallo",
    "Johnson",
    "Larsen",
    "Dubois",
    "Rahman",
    "Moreau",
    "Becker",
    "Rossi",
    "Kim",
    "Okoye",
    "Novak",
    "Fernandez",
    "Singh",
]


@dataclass(frozen=True)
class Condition:
    """A diagnosis and the imaging a patient with it typically gets (all invented)."""

    code: str
    text: str
    weight: int
    studies: tuple[tuple[str, str, str, str | None, str, str], ...]
    # (modality, study description, series description, body part, manufacturer, model)
    order: tuple[str, str] | None = None  # (order code, order text)
    labs: bool = False


CONDITIONS: tuple[Condition, ...] = (
    Condition(
        "C34.90",
        "Malignant neoplasm of unspecified part of bronchus or lung",
        18,
        (
            ("CT", "CT CHEST W CONTRAST", "Axial 1.0 Lung", "CHEST", "SIEMENS", "SOMATOM Force"),
            ("PT", "PET CT WHOLE BODY", "PET AC", "WHOLEBODY", "SIEMENS", "Biograph Vision"),
        ),
        order=("CTCHEST", "CT chest with contrast"),
        labs=True,
    ),
    Condition(
        "I63.9",
        "Cerebral infarction, unspecified",
        16,
        (
            (
                "CT",
                "CT HEAD WITHOUT CONTRAST",
                "Brain 5mm",
                "HEAD",
                "GE MEDICAL SYSTEMS",
                "Revolution CT",
            ),
            (
                "MR",
                "MR BRAIN W WO CONTRAST",
                "DWI AX",
                "BRAIN",
                "GE MEDICAL SYSTEMS",
                "SIGNA Premier",
            ),
        ),
        order=("CTHEAD", "CT head without contrast"),
    ),
    Condition(
        "J18.9",
        "Pneumonia, unspecified organism",
        16,
        (
            ("CR", "CR CHEST PA AND LATERAL", "PA", "CHEST", "FUJIFILM Corporation", "FCR"),
            (
                "CT",
                "CT CHEST W/O CONTRAST",
                "Axial 3.0 Soft",
                "CHEST",
                "Canon Medical Systems",
                "Aquilion ONE",
            ),
        ),
        labs=True,
    ),
    Condition(
        "J44.9",
        "Chronic obstructive pulmonary disease, unspecified",
        10,
        (
            ("CT", "CT CHEST LOW DOSE", "Lung 1mm", "CHEST", "SIEMENS", "SOMATOM Force"),
            ("CR", "CR CHEST PA", "PA", "CHEST", "FUJIFILM Corporation", "FCR"),
        ),
    ),
    Condition(
        "C22.0",
        "Liver cell carcinoma",
        8,
        (
            (
                "CT",
                "CT ABDOMEN PELVIS W CONTRAST",
                "Portal venous",
                "ABDOMEN",
                "SIEMENS",
                "SOMATOM Force",
            ),
            ("MR", "MR ABDOMEN W WO CONTRAST", "T1 VIBE", "ABDOMEN", "Philips", "Ingenia"),
        ),
        order=("CTABD", "CT abdomen and pelvis with contrast"),
        labs=True,
    ),
    Condition(
        "C71.9",
        "Malignant neoplasm of brain, unspecified",
        8,
        (
            ("MR", "MR BRAIN W WO CONTRAST", "T1 AX POST", "BRAIN", "SIEMENS", "MAGNETOM Vida"),
            (
                "CT",
                "CT HEAD WITHOUT CONTRAST",
                "Brain 5mm",
                "HEAD",
                "GE MEDICAL SYSTEMS",
                "Revolution CT",
            ),
        ),
        order=("MRBRAIN", "MR brain with and without contrast"),
    ),
    Condition(
        "I10",
        "Essential (primary) hypertension",
        14,
        (
            ("CR", "CR CHEST PA", "PA", "CHEST", "FUJIFILM Corporation", "FCR"),
            (
                "CT",
                "CT ABDOMEN PELVIS",
                "Axial 5mm",
                "ABDOMEN",
                "GE MEDICAL SYSTEMS",
                "Revolution CT",
            ),
        ),
    ),
    Condition(
        "M17.9",
        "Osteoarthritis of knee, unspecified",
        10,
        (
            ("MR", "MR KNEE LEFT", "PD FS SAG", "KNEE", "Philips", "Ingenia"),
            ("CR", "CR KNEE 2 VIEWS", "AP", "KNEE", "FUJIFILM Corporation", "FCR"),
        ),
    ),
)


@dataclass
class Case:
    """One synthetic patient: identity, diagnosis and imaging studies."""

    patient: FakePatient
    condition: Condition
    studies: list[StudySpec]


def generate_cases(count: int, seed: int = 2024) -> list[Case]:
    """``count`` invented patients, each with a diagnosis and one or two matching studies.

    Deterministic for a seed (same names, MRNs, dates and UIDs every run, so re-seeding is
    idempotent downstream). MRNs start at ``MRN5`` + seed so they never collide with the default
    cohort's. About one in twelve patients is over 89 to exercise the age cap.
    """
    rng = random.Random(seed)
    cases: list[Case] = []
    for n in range(count):
        sex = rng.choice(["M", "F"])
        name = f"{FAMILY_NAMES[(n * 7 + seed) % len(FAMILY_NAMES)]}^{rng.choice(GIVEN_NAMES[sex])}"
        age_years = rng.randint(90, 96) if rng.random() < 0.08 else rng.randint(28, 88)
        first_study = date(2019, 1, 1) + timedelta(days=rng.randrange(365 * 4))
        birth = date(first_study.year - age_years, rng.randint(1, 12), rng.randint(1, 28))
        patient = FakePatient(name, f"MRN5{seed % 100:02d}{n:04d}", sex, birth)
        condition = rng.choices(CONDITIONS, weights=[c.weight for c in CONDITIONS])[0]
        studies = []
        for k in range(2 if rng.random() < 0.3 else 1):
            modality, description, series_desc, body_part, maker, model = condition.studies[k]
            series = [SeriesSpec(modality, series_desc, body_part, 1 if modality == "CR" else 6)]
            if modality in ("CT", "MR") and rng.random() < 0.4:
                series.append(SeriesSpec(modality, f"{series_desc} recon", body_part, 6))
            spec = StudySpec(
                patient,
                first_study + timedelta(days=k * rng.randint(20, 120)),
                description,
                series,
                manufacturer=maker,
                model=model,
                accession=f"ACC5{seed % 100:02d}{n:04d}{k}",
            )
            spec.study_uid = generate_uid(
                prefix=UID_ROOT, entropy_srcs=[str(seed), "case", str(n), str(k)]
            )
            for j, one in enumerate(series):
                one.series_uid = generate_uid(
                    prefix=UID_ROOT, entropy_srcs=[str(seed), "case-series", str(n), str(k), str(j)]
                )
            studies.append(spec)
        cases.append(Case(patient, condition, studies))
    return cases


def _phantom(modality: str, body_part: str | None, size: int, z: float) -> bytes:
    """Signed 16-bit phantom: body ellipse, lungs for chest, skull ring for head."""
    out = bytearray()
    cx = cy = size / 2
    chest = (body_part or "").upper() in {"CHEST", "WHOLEBODY", "ABDOMEN"}
    head = (body_part or "").upper() in {"HEAD", "BRAIN"}
    air, soft, lung, bone = (-1000, 40, -820, 900) if modality == "CT" else (0, 400, 60, 900)
    for y in range(size):
        for x in range(size):
            dx, dy = (x - cx) / (size * 0.42), (y - cy) / (size * (0.30 if chest else 0.40))
            r = dx * dx + dy * dy
            value = air
            if r <= 1.0:
                value = soft
                if head and r > 0.82:
                    value = bone
                if chest:
                    for side in (-1, 1):
                        lx = (x - (cx + side * size * 0.18)) / (size * 0.13)
                        ly = (y - cy) / (size * 0.20 * (1 + 0.1 * math.sin(z)))
                        if lx * lx + ly * ly <= 1.0:
                            value = lung
                    if abs(x - cx) < size * 0.03 and y > cy:
                        value = bone
            out += struct.pack("<h", value)
    return bytes(out)


def make_instance(study: StudySpec, series: SeriesSpec, index: int, size: int = 96) -> Dataset:
    sop_uid = str(generate_uid(prefix=UID_ROOT, entropy_srcs=[series.series_uid, str(index)]))
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = UID(SOP_CLASS[series.modality])
    meta.MediaStorageSOPInstanceUID = UID(sop_uid)
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID

    ds = Dataset()
    ds.file_meta = meta
    ds.SpecificCharacterSet = study.patient.charset
    ds.SOPClassUID = SOP_CLASS[series.modality]
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study.study_uid
    ds.SeriesInstanceUID = series.series_uid
    ds.FrameOfReferenceUID = generate_uid(prefix=UID_ROOT, entropy_srcs=[series.series_uid, "for"])
    ds.PatientName = study.patient.name
    ds.PatientID = study.patient.mrn
    ds.PatientBirthDate = study.patient.birth_date.strftime("%Y%m%d")
    ds.PatientSex = study.patient.sex
    ds.PatientAddress = "742 Evergreen Terrace, Springfield"
    ds.PatientTelephoneNumbers = "555-867-5309"
    ds.OtherPatientIDs = "SSN 123-45-6789"
    ds.StudyDate = study.study_date.strftime("%Y%m%d")
    ds.SeriesDate = ds.StudyDate
    ds.ContentDate = ds.StudyDate
    ds.StudyTime = "093000"
    ds.AccessionNumber = study.accession
    ds.StudyID = "1"
    ds.ReferringPhysicianName = "House^Gregory"
    ds.PerformingPhysicianName = "Quinn^Michaela"
    ds.OperatorsName = "Tech^Terry"
    ds.InstitutionName = study.institution
    ds.InstitutionAddress = "1 Hospital Way, Springfield"
    ds.StationName = "CT-ROOM-3"
    ds.DeviceSerialNumber = "SN-998877"
    ds.Manufacturer = study.manufacturer
    ds.ManufacturerModelName = study.model
    ds.StudyDescription = study.description
    ds.SeriesDescription = series.description
    ds.ProtocolName = series.description
    if series.body_part:
        ds.BodyPartExamined = series.body_part
    ds.Modality = series.modality
    ds.SeriesNumber = study.series.index(series) + 1
    ds.InstanceNumber = index + 1
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.ImageComments = f"Patient {study.patient.name.replace('^', ' ')} tolerated well"
    ds.PatientPosition = "HFS"
    ds.ImagePositionPatient = [-120.0, -120.0, float(index) * 2.5]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.SliceThickness = 2.5
    ds.SliceLocation = float(index) * 2.5
    ds.PixelSpacing = [2.5, 2.5]
    if series.modality == "MR":
        ds.MagneticFieldStrength = 3.0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = size
    ds.Columns = size
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 1
    ds.RescaleIntercept = 0
    ds.RescaleSlope = 1
    ds.WindowCenter = 40 if series.modality == "CT" else 300
    ds.WindowWidth = 400 if series.modality == "CT" else 800
    ds.add_new(0x00091010, "LO", "PRIVATE VENDOR DATA")  # private tag: must be removed
    ds.PixelData = _phantom(series.modality, series.body_part, size, index / 2.0)
    return ds


def write_cohort(
    out_dir: Path, studies: list[StudySpec] | None = None, size: int = 96
) -> list[Path]:
    studies = studies if studies is not None else default_cohort()
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for s_i, study in enumerate(studies):
        for series in study.series:
            for i in range(series.slices):
                ds = make_instance(study, series, i, size)
                path = out_dir / f"study{s_i:02d}" / f"{series.series_uid[-8:]}_{i:03d}.dcm"
                path.parent.mkdir(parents=True, exist_ok=True)
                ds.save_as(path, enforce_file_format=True)
                paths.append(path)
    return paths


def write_corrupt(out_dir: Path) -> list[Path]:
    """Two bad inputs: random bytes, and a DICOM-preamble file with a truncated header."""
    out_dir.mkdir(parents=True, exist_ok=True)
    junk = out_dir / "corrupt_random.dcm"
    junk.write_bytes(bytes(random.Random(7).randrange(256) for _ in range(2048)))
    truncated = out_dir / "corrupt_truncated.dcm"
    truncated.write_bytes(b"\x00" * 128 + b"DICM" + b"\x02\x00\x10\x00UI\x40\x00" + b"1.2.3")
    return [junk, truncated]


def main() -> int:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--size", type=int, default=96)
    parser.add_argument("--corrupt", action="store_true", help="also write two corrupt files")
    args = parser.parse_args()
    paths = write_cohort(args.out, size=args.size)
    if args.corrupt:
        paths += write_corrupt(args.out / "corrupt")
    print(f"wrote {len(paths)} files to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
