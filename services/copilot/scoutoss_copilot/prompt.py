"""Prompt construction: index schema + few-shot examples + recent turns + the question.

The LLM never sees a document. It sees field names, field descriptions, a handful of
de-identified example *values* for low-cardinality fields (modalities, body parts, site
codes...), previous plans in this session and the redacted question.
"""

from __future__ import annotations

import json
from typing import Any

from scoutoss_copilot.llm import Message

SYSTEM_TEMPLATE = """You translate research questions about a de-identified medical imaging \
and clinical data lake into a JSON query plan for OpenSearch. Reply with ONE JSON object and \
nothing else.

Plan format:
{{"steps": [{{"index": "<index>", "query": <OpenSearch query DSL>, "size": 10000,
              "description": "<what this step selects>"}}],
  "combine": "intersect" | "union",      // how step results combine on patient_pseudonym
  "subject": "patients" | "studies",     // what the user is counting
  "charts": ["modality"|"sex"|"age"|"body_part"|"year"|"diagnosis"|"manufacturer"],
  "base_cohort_id": null | "<id of a previous cohort when the user refers to 'those'>",
  "explanation": "<one sentence>"}}

Rules:
- Read-only. Never use scripts, updates, deletes, *_by_query or any API other than search.
- Use only the indices and fields listed below. Imaging and clinical data are joined by \
putting each in its own step and combining with "intersect" on patient_pseudonym.
- Allowed clauses: bool, term, terms, range, match, match_phrase, exists, prefix, wildcard \
(no leading wildcard). Keyword values are upper-case for modalities and body parts.
- Ages are years at study (patient_age) and are capped at 90. Dates are shifted per project.
- size must not exceed 10000.

Indices and fields:
{schema}

Examples:
{examples}
"""

FEW_SHOT: list[tuple[str, dict[str, Any]]] = [
    (
        "how many CT studies",
        {
            "steps": [
                {
                    "index": "dicom_studies",
                    "query": {"term": {"modalities": "CT"}},
                    "size": 10000,
                    "description": "CT studies",
                }
            ],
            "combine": "intersect",
            "subject": "studies",
            "charts": ["body_part"],
            "explanation": "Counts studies containing a CT series.",
        },
    ),
    (
        "female patients over 60 with an MR brain, by age",
        {
            "steps": [
                {
                    "index": "dicom_studies",
                    "size": 10000,
                    "description": "MR head studies, F, >60",
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"modalities": "MR"}},
                                {"term": {"body_parts": "HEAD"}},
                                {"term": {"patient_sex": "F"}},
                                {"range": {"patient_age": {"gt": 60}}},
                            ]
                        }
                    },
                }
            ],
            "combine": "intersect",
            "subject": "patients",
            "charts": ["age"],
            "explanation": "Female patients older than 60 with an MR of the head.",
        },
    ),
    (
        "patients under 85 with lung cancer AND CT chest",
        {
            "steps": [
                {
                    "index": "dicom_studies",
                    "size": 10000,
                    "description": "CT chest studies, age < 85",
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"modalities": "CT"}},
                                {"term": {"body_parts": "CHEST"}},
                                {"range": {"patient_age": {"lt": 85}}},
                            ]
                        }
                    },
                },
                {
                    "index": "clinical_events",
                    "size": 10000,
                    "description": "lung cancer diagnoses",
                    "query": {"term": {"diagnosis_category": "LUNG_CANCER"}},
                },
            ],
            "combine": "intersect",
            "subject": "patients",
            "charts": ["age", "sex"],
            "explanation": "Patients younger than 85 with a lung-cancer diagnosis and a CT chest.",
        },
    ),
]


def render_schema(schema: dict[str, Any]) -> str:
    lines: list[str] = []
    for index, info in schema.get("indices", {}).items():
        lines.append(f"- {index} ({info.get('doc_count', '?')} docs)")
        examples = info.get("examples", {})
        for field, description in info.get("fields", {}).items():
            sample = examples.get(field)
            hint = f" e.g. {', '.join(map(str, sample[:8]))}" if sample else ""
            lines.append(f"    {field}: {description}{hint}")
    return "\n".join(lines)


def allow_terms(schema: dict[str, Any]) -> set[str]:
    """De-identified vocabulary that must survive PHI name detection."""
    terms: set[str] = set()
    for info in schema.get("indices", {}).values():
        for values in info.get("examples", {}).values():
            terms.update(str(v) for v in values)
    return terms


def build_messages(
    question: str,
    schema: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
) -> list[Message]:
    examples = "\n".join(
        f"Q: {q}\nA: {json.dumps(plan, separators=(',', ':'))}" for q, plan in FEW_SHOT
    )
    messages = [
        Message("system", SYSTEM_TEMPLATE.format(schema=render_schema(schema), examples=examples))
    ]
    for turn in history or []:
        messages.append(Message("user", f"QUESTION: {turn['question']}"))
        messages.append(
            Message("assistant", json.dumps({**turn["plan"], "cohort_id": turn.get("cohort_id")}))
        )
    messages.append(Message("user", f"QUESTION: {question}"))
    return messages
