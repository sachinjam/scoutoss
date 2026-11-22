"""ScoutOSS QC pipeline (XNAT Container Service demo).

Reads every DICOM file under the mounted session directory, computes per-scan statistics
(instances, rows/cols, slice thickness, pixel mean/std, a crude SNR) and writes an
``xnat:qcAssessmentData`` XML that the Container Service "Assessor" output handler
attaches to the session. Never prints header values (only counts and numbers).

It also writes the same result as a DICOM Secondary Capture page into ``OUTPUT_DIR/dicom``, which
XNAT stores as the session resource ``AI_DICOM``. The page belongs to the source study (identity
copied from one of its instances, so it is exactly as de-identified as the study) and the
``dicom-export`` service sends everything in that resource to the configured DICOM destination.

Usage: qc.py INPUT_DIR OUTPUT_DIR SESSION_ID SESSION_LABEL PROJECT
"""

from __future__ import annotations

import json
import math
import struct
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pydicom
from PIL import Image, ImageDraw, ImageFont
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    ExplicitVRLittleEndian,
    SecondaryCaptureImageStorage,
    generate_uid,
)

# Copied from a source instance so the result lands in the same patient and study.
IDENTITY = (
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "PatientSex",
    "StudyInstanceUID",
    "StudyID",
    "AccessionNumber",
    "StudyDate",
    "StudyTime",
    "StudyDescription",
    "ReferringPhysicianName",
    "PatientIdentityRemoved",
    "DeidentificationMethod",
)
SERIES_NUMBER = 900  # ScoutOSS derived results use series 900-909
SERIES_DESCRIPTION = "ScoutOSS QC summary"


def stats(values: list[int]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, math.sqrt(var)


def scan_stats(files: list[Path]) -> dict[str, float | int | str]:
    sample: list[int] = []
    rows = cols = 0
    thickness = 0.0
    for f in files[:: max(1, len(files) // 8)]:
        ds = pydicom.dcmread(f)
        rows, cols = int(ds.get("Rows", 0)), int(ds.get("Columns", 0))
        thickness = float(ds.get("SliceThickness", 0) or 0)
        data = ds.get("PixelData")
        if data and int(ds.get("BitsAllocated", 16)) == 16:
            n = len(data) // 2
            fmt = f"<{n}h" if int(ds.get("PixelRepresentation", 0)) else f"<{n}H"
            sample.extend(struct.unpack(fmt, data[: n * 2])[::7])
    mean, std = stats(sample)
    return {
        "instances": len(files),
        "rows": rows,
        "cols": cols,
        "slice_thickness": thickness,
        "mean": round(mean, 2),
        "std": round(std, 2),
        "min": min(sample) if sample else 0,
        "max": max(sample) if sample else 0,
        "snr": round(abs(mean) / std, 3) if std else 0.0,
        "status": "pass" if files and rows > 0 else "fail",
    }


def assessor_xml(
    label: str,
    project: str,
    session_id: str,
    results: dict[str, dict[str, float | int | str]],
    overall: str,
    today: str,
) -> str:
    """xnat:qcAssessmentData (QCAssessment): type is an attribute, per-scan statistics go in
    scanStatistics (xnat:statisticsData), the overall rating and per-scan summary as JSON in the
    standard note. Custom ``xnat:fields`` are avoided on purpose: when two assessors are finalised
    at the same moment on a freshly started XNAT, its XML reader intermittently rejects
    ``fields/field`` ("Invalid XML 'xnat:experimentData_field:fields/field'") and the upload fails."""
    scans = "".join(
        f"<xnat:scan id={quoteattr(scan)}>"
        f'<xnat:scanStatistics xsi:type="xnat:statisticsData">'
        f"<xnat:mean>{r['mean']}</xnat:mean><xnat:snr>{r['snr']}</xnat:snr>"
        f"<xnat:min>{r['min']}</xnat:min><xnat:max>{r['max']}</xnat:max>"
        f"<xnat:stddev>{r['std']}</xnat:stddev></xnat:scanStatistics></xnat:scan>"
        for scan, r in results.items()
    )
    summary = json.dumps(
        {
            "rating": overall,
            "scans": len(results),
            "per_scan": {
                s: {k: v for k, v in r.items() if k in ("instances", "rows", "cols", "status")}
                for s, r in results.items()
            },
        }
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xnat:QCAssessment xmlns:xnat="http://nrg.wustl.edu/xnat" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'ID={quoteattr(label)} label={quoteattr(label)} project={quoteattr(project)} type="ScoutOSS-QC">'
        f"<xnat:date>{today}</xnat:date>"
        f"<xnat:note>{escape(summary)[:1000]}</xnat:note>"
        f"<xnat:imageSession_ID>{escape(session_id)}</xnat:imageSession_ID>"
        f"<xnat:scans>{scans}</xnat:scans>"
        "</xnat:QCAssessment>\n"
    )


Row = tuple[str, ...]
COLUMNS = (40, 150, 270, 400, 530, 650, 770, 870)  # x position of each table column


def summary_lines(
    label: str, results: dict[str, dict[str, float | int | str]], overall: str, now: datetime
) -> list[str | Row]:
    """Text lines, and tuples for table rows (drawn in fixed columns: the font is proportional)."""
    lines: list[str | Row] = [
        "ScoutOSS QC summary",
        "",
        f"Session   {label}",
        f"Result    {overall.upper()}",
        f"Created   {now.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        ("Scan", "Images", "Matrix", "Slice mm", "Mean", "SD", "SNR", "Status"),
    ]
    for scan, r in results.items():
        cells = ("instances", "slice_thickness", "mean", "std", "snr", "status")
        values = [str(r[k]) for k in cells]
        lines.append((scan[:8], values[0], f"{r['rows']}x{r['cols']}", *values[1:]))
    return [*lines, "", "Research use only. Not for diagnosis."]


def render_page(lines: list[str | Row], overall: str) -> Image.Image:
    """White page with the summary text; a green or red bar shows the overall result."""
    size = 22
    font = ImageFont.load_default(size=size)
    text = [line for line in lines if isinstance(line, str)]
    width = max(1000, int(max(font.getlength(line) for line in text)) + 80)
    height = 80 + (size + 10) * len(lines)
    page = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle([0, 0, width, 14], fill=(0, 128, 60) if overall == "pass" else (190, 30, 30))
    for n, line in enumerate(lines):
        y = 40 + n * (size + 10)
        cells = zip(COLUMNS, line, strict=False) if isinstance(line, tuple) else [(40, line)]
        for x, cell in cells:
            draw.text((x, y), cell, fill="black", font=font)
    return page


def summary_dicom(reference: Dataset, page: Image.Image, now: datetime) -> Dataset:
    """One Secondary Capture instance in the reference instance's study. UIDs are derived from
    the study, so running QC again replaces the page at the destination instead of adding one."""
    study_uid = str(reference.StudyInstanceUID)
    ds = Dataset()
    for name in IDENTITY:
        if name in reference:
            ds[name] = reference[name]
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = generate_uid(entropy_srcs=[study_uid, "scoutoss-qc", "page-1"])
    ds.SeriesInstanceUID = generate_uid(entropy_srcs=[study_uid, "scoutoss-qc", "series"])
    ds.Modality = "OT"
    ds.ConversionType = "WSD"
    ds.SeriesNumber = SERIES_NUMBER
    ds.InstanceNumber = 1
    ds.SeriesDescription = SERIES_DESCRIPTION
    ds.Manufacturer = "ScoutOSS"
    ds.ImageType = ["DERIVED", "SECONDARY"]
    ds.BurnedInAnnotation = "NO"
    ds.PatientOrientation = ""
    ds.ContentDate = ds.SeriesDate = now.strftime("%Y%m%d")
    ds.ContentTime = ds.SeriesTime = now.strftime("%H%M%S")
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "RGB"
    ds.PlanarConfiguration = 0
    ds.Rows, ds.Columns = page.height, page.width
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    pixels = page.tobytes()
    ds.PixelData = pixels + (b"\0" if len(pixels) % 2 else b"")
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return ds


def write_summary_dicom(
    reference_file: Path,
    out_dir: Path,
    label: str,
    results: dict[str, dict[str, float | int | str]],
    overall: str,
    now: datetime,
) -> Path:
    reference = pydicom.dcmread(reference_file, stop_before_pixels=True)
    page = render_page(summary_lines(label, results, overall, now), overall)
    # A new name per run: dicom-export tells files apart by name and size (XNAT has no checksum
    # for them), and the UIDs stay the same, so the destination replaces the earlier page.
    target = out_dir / "dicom" / f"scoutoss-qc-summary-{now.strftime('%Y%m%dT%H%M%S')}.dcm"
    target.parent.mkdir(parents=True, exist_ok=True)
    summary_dicom(reference, page, now).save_as(target, enforce_file_format=True)
    return target


def main(argv: list[str]) -> int:
    in_dir, out_dir, session_id, label, project = (
        Path(argv[1]),
        Path(argv[2]),
        argv[3],
        argv[4],
        argv[5],
    )
    by_scan: dict[str, list[Path]] = defaultdict(list)
    for f in sorted(in_dir.rglob("*.dcm")):
        parts = f.relative_to(in_dir).parts
        if "SCANS" not in parts:
            continue  # session resources, e.g. the AI_DICOM page written by an earlier run
        by_scan[parts[parts.index("SCANS") + 1]].append(f)
    results = {scan: scan_stats(files) for scan, files in sorted(by_scan.items())}
    overall = "pass" if results and all(r["status"] == "pass" for r in results.values()) else "fail"
    now = datetime.now(UTC)
    qc_label = f"{label}_SCOUTQC_{now.strftime('%Y%m%d%H%M%S')}"[:64]
    xml = assessor_xml(qc_label, project, session_id, results, overall, now.date().isoformat())
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "qc.xml").write_text(xml)
    first = next((files[0] for files in by_scan.values() if files), None)
    if first is not None:
        write_summary_dicom(first, out_dir, label, results, overall, now)
    print(
        json.dumps(
            {
                "msg": "qc complete",
                "scans": len(results),
                "rating": overall,
                "dicom_summary": first is not None,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
