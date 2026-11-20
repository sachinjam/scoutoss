"""ScoutOSS QC pipeline (XNAT Container Service demo).

Reads every DICOM file under the mounted session directory, computes per-scan statistics
(instances, rows/cols, slice thickness, pixel mean/std, a crude SNR) and writes an
``xnat:qcAssessmentData`` XML that the Container Service "Assessor" output handler
attaches to the session. Never prints header values (only counts and numbers).

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
    scanStatistics (xnat:statisticsData), the overall rating in a custom field."""
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
            s: {k: v for k, v in r.items() if k in ("instances", "rows", "cols", "status")}
            for s, r in results.items()
        }
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xnat:QCAssessment xmlns:xnat="http://nrg.wustl.edu/xnat" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'ID={quoteattr(label)} label={quoteattr(label)} project={quoteattr(project)} type="ScoutOSS-QC">'
        f"<xnat:date>{today}</xnat:date>"
        f"<xnat:note>{escape(summary)[:1000]}</xnat:note>"
        f'<xnat:fields><xnat:field name="rating">{overall}</xnat:field>'
        f'<xnat:field name="scans">{len(results)}</xnat:field></xnat:fields>'
        f"<xnat:imageSession_ID>{escape(session_id)}</xnat:imageSession_ID>"
        f"<xnat:scans>{scans}</xnat:scans>"
        "</xnat:QCAssessment>\n"
    )


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
        scan = parts[parts.index("SCANS") + 1] if "SCANS" in parts else "unknown"
        by_scan[scan].append(f)
    results = {scan: scan_stats(files) for scan, files in sorted(by_scan.items())}
    overall = "pass" if results and all(r["status"] == "pass" for r in results.values()) else "fail"
    now = datetime.now(UTC)
    qc_label = f"{label}_SCOUTQC_{now.strftime('%Y%m%d%H%M%S')}"[:64]
    xml = assessor_xml(qc_label, project, session_id, results, overall, now.date().isoformat())
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "qc.xml").write_text(xml)
    print(json.dumps({"msg": "qc complete", "scans": len(results), "rating": overall}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
