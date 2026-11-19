#!/usr/bin/env python3
"""Write one fresh synthetic CT chest study (new UIDs every run) for e2e tests.

Usage: make_study.py OUT_DIR  -> prints the (pre-de-identification) StudyInstanceUID.
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "indexer"))

from pydicom.uid import generate_uid

from scoutoss_indexer.synthetic import UID_ROOT, FakePatient, SeriesSpec, StudySpec, write_cohort

stamp = str(time.time_ns())
study = StudySpec(
    FakePatient("Tester^Eve", f"E2E{stamp[-9:]}", "F", date(1961, 4, 2)),
    date(2023, 6, 1),
    "CT CHEST W CONTRAST",
    [
        SeriesSpec(
            "CT", "Axial 2mm", "CHEST", 6, generate_uid(prefix=UID_ROOT, entropy_srcs=[stamp, "s"])
        )
    ],
    study_uid=generate_uid(prefix=UID_ROOT, entropy_srcs=[stamp]),
)
write_cohort(Path(sys.argv[1]), [study], size=64)
print(study.study_uid)
