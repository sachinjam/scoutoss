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
            fmt = (
                f"<{len(data) // 2}h"
                if int(ds.get("PixelRepresentation", 0))
                else f"<{len(data) // 2}H"
            )
            sample.extend(struct.unpack(fmt, data[: (len(data) // 2) * 2])[::7])
    mean, std = stats(sample)
    snr = round(abs(mean) / std, 3) if std else 0.0
    return {
        "instances": len(files),
        "rows": rows,
        "cols": cols,
        "slice_thickness": thickness,
        "mean": round(mean, 2),
        "std": round(std, 2),
        "snr": snr,
        "status": "pass" if len(files) > 0 and rows > 0 else "fail",
    }


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
    qc_label = f"{label}_SCOUTQC_{now.strftime('%Y%m%d%H%M%S')}"
    scans_xml = "".join(
        f"<xnat:scan><xnat:imageScan_ID>{escape(s)}</xnat:imageScan_ID><xnat:rating>{r['status']}</xnat:rating>"
        f"<xnat:comments>{escape(json.dumps(r))}</xnat:comments></xnat:scan>"
        for s, r in results.items()
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xnat:QCAssessment xmlns:xnat="http://nrg.wustl.edu/xnat" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f"ID={quoteattr(qc_label)} label={quoteattr(qc_label)} project={quoteattr(project)}>"
        f"<xnat:date>{now.date().isoformat()}</xnat:date>"
        f"<xnat:imageSession_ID>{escape(session_id)}</xnat:imageSession_ID>"
        "<xnat:type>ScoutOSS-QC</xnat:type>"
        f"<xnat:rating>{overall}</xnat:rating>"
        f"<xnat:scans>{scans_xml}</xnat:scans>"
        "</xnat:QCAssessment>\n"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "qc.xml").write_text(xml)
    print(json.dumps({"msg": "qc complete", "scans": len(results), "rating": overall}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
