from __future__ import annotations

from typing import Any

import pytest

from scoutoss_copilot.clients import static_schema
from scoutoss_copilot.cohorts import ConversationMemory, MemoryCohortRepo
from scoutoss_copilot.executor import Executor
from scoutoss_copilot.llm import FakeLLM
from scoutoss_copilot.service import Copilot
from tests.support.fake_opensearch import FakeOpenSearch
from tests.support.plans import plan_json

STUDIES = [
    # pseudonym, study, modalities, body parts, age, sex, year, experiment
    ("SPA", "1.1", ["CT"], ["CHEST"], 70.0, "M", 2021, "E1"),
    ("SPA", "1.2", ["CT"], ["CHEST"], 70.3, "M", 2021, "E2"),
    ("SPB", "2.1", ["MR"], ["HEAD"], 45.0, "F", 2020, "E3"),
    ("SPC", "3.1", ["CT"], ["CHEST"], 90.0, "F", 2022, "E4"),
    ("SPD", "4.1", ["CT", "PT"], ["WHOLEBODY"], 63.0, "M", 2022, "E5"),
    ("SPE", "5.1", ["CR"], ["CHEST"], 30.0, "F", 2019, None),
]
CLINICAL = [
    ("SPA", "LUNG_CANCER", "C34.90"),
    ("SPC", "LUNG_CANCER", "C34.1"),
    ("SPB", "STROKE", "I63.9"),
    ("SPD", "LUNG_CANCER", "C34.2"),
]


@pytest.fixture
def populated_os() -> FakeOpenSearch:
    os_ = FakeOpenSearch()
    for pid, uid, mods, parts, age, sex, year, exp in STUDIES:
        os_.index(
            "dicom_studies",
            {
                "study_uid": uid,
                "patient_pseudonym": pid,
                "modalities": mods,
                "body_parts": parts,
                "patient_age": age,
                "patient_sex": sex,
                "study_year": year,
                "study_date": f"{year}-03-01",
                "project": "SCOUT_DEMO",
                "xnat_experiment_id": exp,
                "manufacturers": ["SIEMENS"],
                "viewer_url": f"https://xnat/VIEWER/?experimentId={exp}",
            },
            id=uid,
        )
    for i, (pid, cat, code) in enumerate(CLINICAL):
        os_.index(
            "clinical_events",
            {
                "event_id": f"ev{i}",
                "patient_pseudonym": pid,
                "diagnosis_category": cat,
                "diagnosis_code": code,
                "event_type": "diagnosis",
                "project": "SCOUT_DEMO",
            },
            id=f"ev{i}",
        )
    os_.index(
        "dicom_patients",
        {
            "patient_pseudonym": "SPA",
            "patient_sex": "M",
            "age_latest": 70.3,
            "projects": ["SCOUT_DEMO"],
        },
        id="SPA",
    )
    return os_


class StaticSchema:
    def __init__(self) -> None:
        self.schema = static_schema()
        self.schema["indices"]["dicom_studies"]["examples"] = {
            "modalities": ["CT", "MR"],
            "manufacturers": ["SIEMENS", "Acme Zentrix"],
        }

    def get(self) -> dict[str, Any]:
        return self.schema


@pytest.fixture
def make_copilot(populated_os: FakeOpenSearch):  # type: ignore[no-untyped-def]
    def _make(llm: Any = None, repo: Any = None) -> Copilot:
        return Copilot(
            llm=llm
            or FakeLLM(
                default=plan_json(
                    {"index": "dicom_studies", "query": {"term": {"modalities": "CT"}}}
                )
            ),
            executor=Executor(populated_os),
            cohorts=repo or MemoryCohortRepo(),
            memory=ConversationMemory(turns=4),
            schema=StaticSchema(),
        )

    return _make
