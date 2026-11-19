"""Offline, rule-based "LLM": turns common cohort questions into a query plan.

It is the default provider in dev and air-gapped deployments — no network, no model, no
data leaves the host — and it speaks the same protocol as a real model (chat messages in,
plan JSON out), so it exercises the identical validation/execution path. It understands
modality, body part, sex, age bounds, study year, manufacturer, diagnosis categories,
"by X" breakdowns and follow-ups ("of those, how many are female?").
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from scoutoss_common.catalog import CLINICAL_INDEX, PATIENTS_INDEX, STUDIES_INDEX
from scoutoss_copilot.llm import Message

MODALITIES: tuple[tuple[str, str], ...] = (
    (r"\bct\b|computed tomography|cat scan", "CT"),
    (r"\bmri?\b|magnetic resonance", "MR"),
    (r"\bpet\b|\bpt\b|positron", "PT"),
    (r"\bcr\b|x-?rays?|radiograph", "CR"),
    (r"\bdx\b", "DX"),
    (r"\bus\b|ultrasound", "US"),
    (r"\bmg\b|mammo", "MG"),
    (r"\bnm\b|nuclear medicine|spect", "NM"),
)
BODY_PARTS: tuple[tuple[str, str], ...] = (
    (r"chest|thorax|thoracic|lungs?\b(?! cancer)", "CHEST"),
    (r"\bhead\b|brain|cranial", "HEAD"),
    (r"abdom", "ABDOMEN"),
    (r"pelvi", "PELVIS"),
    (r"knee", "KNEE"),
    (r"spine|spinal|lumbar|cervical", "SPINE"),
    (r"breast", "BREAST"),
    (r"whole[- ]?body", "WHOLEBODY"),
    (r"heart|cardiac", "HEART"),
)
DIAGNOSES: tuple[tuple[str, str], ...] = (
    (r"lung (cancer|carcinoma|tumou?r|neoplasm)|nsclc|sclc", "LUNG_CANCER"),
    (r"breast (cancer|carcinoma)", "BREAST_CANCER"),
    (r"prostate (cancer|carcinoma)", "PROSTATE_CANCER"),
    (r"colo(rectal|n) (cancer|carcinoma)", "COLORECTAL_CANCER"),
    (r"pancrea\w* (cancer|carcinoma)", "PANCREATIC_CANCER"),
    (r"brain (cancer|tumou?r)|glioma|glioblastoma", "BRAIN_CANCER"),
    (r"lymphoma", "LYMPHOMA"),
    (r"(lung|pulmonary) nodules?", "LUNG_NODULE"),
    (r"stroke", "STROKE"),
    (r"pneumonia", "PNEUMONIA"),
    (r"hypertension", "HYPERTENSION"),
    (r"copd", "COPD"),
    (r"diabet", "DIABETES_T2"),
)
MANUFACTURERS: tuple[tuple[str, str], ...] = (
    (r"siemens", "SIEMENS"),
    (r"\bge\b|general electric", "GE MEDICAL SYSTEMS"),
    (r"philips", "Philips"),
    (r"canon|toshiba", "Canon Medical Systems"),
    (r"fuji", "FUJIFILM Corporation"),
)
CHART_HINTS: tuple[tuple[str, str], ...] = (
    (r"by modalit|per modalit|modality (breakdown|distribution)", "modality"),
    (r"by (sex|gender)|male.*female|sex (breakdown|distribution)|gender", "sex"),
    (r"\bage\b.*(distribution|histogram|breakdown)|by age|age group", "age"),
    (r"body ?parts?|anatom", "body_part"),
    (r"per year|by year|over time|trend|timeline", "year"),
    (r"by diagnos|diagnos\w* (breakdown|distribution)", "diagnosis"),
    (r"by (manufacturer|vendor|scanner)|manufacturers|vendors", "manufacturer"),
)
FOLLOW_UP = re.compile(
    r"^(and |also |of (those|these|them)|among (those|these|them)|"
    r"how many of (those|these|them)|(those|these|them)\b)|\b(of|among) (those|these|them)\b",
    re.I,
)


def _age_range(q: str) -> dict[str, float]:
    rng: dict[str, float] = {}
    m = re.search(r"between (\d{1,3}) and (\d{1,3})", q) or re.search(
        r"aged? (\d{1,3}) ?(?:-|to) ?(\d{1,3})", q
    )
    if m:
        return {"gte": float(m.group(1)), "lte": float(m.group(2))}
    m = re.search(r"(under|younger than|below|less than|<) ?(\d{1,3})", q)
    if m:
        rng["lt"] = float(m.group(2))
    m = re.search(
        r"(over|older than|above|more than|greater than|>) ?(\d{1,3})(?! ?(studies|series|patients))",
        q,
    )
    if m:
        rng["gt"] = float(m.group(2))
    m = re.search(r"(at least|>=) ?(\d{1,3})", q)
    if m:
        rng["gte"] = float(m.group(2))
    return rng


def _year_range(q: str) -> dict[str, int]:
    m = re.search(r"\b(?:in|during) (19|20)(\d{2})\b", q)
    if m:
        y = int(m.group(1) + m.group(2))
        return {"gte": y, "lte": y}
    rng: dict[str, int] = {}
    m = re.search(r"\b(?:after|since) (19|20)(\d{2})\b", q)
    if m:
        rng["gt" if "after" in m.group(0) else "gte"] = int(m.group(1) + m.group(2))
    m = re.search(r"\bbefore (19|20)(\d{2})\b", q)
    if m:
        rng["lt"] = int(m.group(1) + m.group(2))
    return rng


def _matches(table: tuple[tuple[str, str], ...], q: str) -> list[str]:
    return list(dict.fromkeys(v for pattern, v in table if re.search(pattern, q)))


def plan_for(question: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    q = question.lower()
    diagnoses = _matches(DIAGNOSES, q)
    if diagnoses:  # "lung cancer" must not also read as body part LUNG
        q_img = re.sub(
            r"(lung|breast|prostate|brain|colon|pancrea\w*) (cancer|carcinoma|tumou?r|neoplasm|nodules?)",
            " ",
            q,
        )
    else:
        q_img = q
    modalities = _matches(MODALITIES, q_img)
    body_parts = _matches(BODY_PARTS, q_img)
    manufacturers = _matches(MANUFACTURERS, q_img)
    sex = (
        "F"
        if re.search(r"\b(female|women|woman)\b", q)
        else "M"
        if re.search(r"\b(male|men|man)\b", q)
        else None
    )
    age = _age_range(q)
    years = _year_range(q)
    charts = _matches(CHART_HINTS, q)
    subject = (
        "studies"
        if re.search(r"\b(studies|exams|scans|series)\b", q) and "patient" not in q
        else "patients"
    )

    imaging_filters: list[dict[str, Any]] = []
    if modalities:
        imaging_filters.append({"terms": {"modalities": modalities}})
    if body_parts:
        imaging_filters.append({"terms": {"body_parts": body_parts}})
    if manufacturers:
        imaging_filters.append({"terms": {"manufacturers": manufacturers}})
    if years:
        imaging_filters.append({"range": {"study_year": years}})

    steps: list[dict[str, Any]] = []
    demographic: list[dict[str, Any]] = []
    if sex:
        demographic.append({"term": {"patient_sex": sex}})
    if age:
        demographic.append({"range": {"patient_age": age}})

    if imaging_filters or not diagnoses:
        filters = imaging_filters + demographic
        steps.append(
            {
                "index": STUDIES_INDEX,
                "description": "imaging studies matching the imaging criteria",
                "query": {"bool": {"filter": filters}} if filters else {"match_all": {}},
                "size": 10000,
            }
        )
    if diagnoses:
        clinical_filters: list[dict[str, Any]] = [{"terms": {"diagnosis_category": diagnoses}}]
        if not steps:  # demographics apply to the clinical record when there is no imaging step
            clinical_filters += demographic
        steps.append(
            {
                "index": CLINICAL_INDEX,
                "description": "patients with a matching diagnosis in HL7 feeds",
                "query": {"bool": {"filter": clinical_filters}},
                "size": 10000,
            }
        )

    plan: dict[str, Any] = {
        "steps": steps,
        "combine": "intersect",
        "join_field": "patient_pseudonym",
        "subject": subject,
        "charts": charts,
        "explanation": "Heuristic plan: "
        + ", ".join(
            p
            for p in (
                f"modality {'/'.join(modalities)}" if modalities else "",
                f"body part {'/'.join(body_parts)}" if body_parts else "",
                f"diagnosis {'/'.join(diagnoses)}" if diagnoses else "",
                f"sex {sex}" if sex else "",
                f"age {age}" if age else "",
                f"year {years}" if years else "",
            )
            if p
        )
        or "all studies",
    }
    if previous and FOLLOW_UP.search(question.strip()) and previous.get("cohort_id"):
        plan["base_cohort_id"] = previous["cohort_id"]
        if not (imaging_filters or diagnoses):
            plan["steps"] = [
                {
                    "index": PATIENTS_INDEX if not demographic else STUDIES_INDEX,
                    "description": "previous cohort, refined",
                    "query": {"bool": {"filter": demographic}}
                    if demographic
                    else {"match_all": {}},
                    "size": 10000,
                }
            ]
    return plan


class HeuristicPlanner:
    """LLMProvider implementation backed by :func:`plan_for`."""

    name = "heuristic"

    def complete(self, messages: Sequence[Message]) -> str:
        question = next((m.content for m in reversed(messages) if m.role == "user"), "")
        previous = None
        for m in reversed(messages[:-1]):
            if m.role == "assistant":
                try:
                    previous = json.loads(m.content)
                except json.JSONDecodeError:
                    previous = None
                break
        question = question.split("QUESTION:", 1)[-1].strip()
        return json.dumps(plan_for(question, previous))
