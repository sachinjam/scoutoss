#!/usr/bin/env python3
"""Generate dashboards/scoutoss.ndjson (OpenSearch Dashboards saved objects).

Run ``python scripts/gen_dashboards.py`` after changing a visualization; the generated
NDJSON is committed and imported on first boot by scripts/bootstrap/opensearch.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent.parent / "dashboards" / "scoutoss.ndjson"
VERSION = "2.19.3"
STUDIES = "scoutoss-studies"

INDEX_PATTERNS = [
    ("scoutoss-studies", "dicom_studies", "study_date"),
    ("scoutoss-series", "dicom_series", "study_date"),
    ("scoutoss-patients", "dicom_patients", None),
    ("scoutoss-clinical", "clinical_events", "event_date"),
    ("scoutoss-audit", "audit", "ts"),
    ("scoutoss-dlq", "indexer_dlq", "ts"),
    ("scoutoss-logs", "logs-*", "ts"),
]


def source(index_ref: bool = True, query: str = "") -> str:
    body: dict[str, Any] = {"query": {"query": query, "language": "kuery"}, "filter": []}
    if index_ref:
        body["indexRefName"] = "kibanaSavedObjectMeta.searchSourceJSON.index"
    return json.dumps(body)


def index_ref(pattern: str = STUDIES) -> list[dict[str, str]]:
    return [
        {
            "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
            "type": "index-pattern",
            "id": pattern,
        }
    ]


def vis(vis_id: str, title: str, state: dict[str, Any], description: str = "") -> dict[str, Any]:
    state = {"title": title, **state}
    return {
        "id": vis_id,
        "type": "visualization",
        "attributes": {
            "title": title,
            "description": description,
            "visState": json.dumps(state),
            "uiStateJSON": "{}",
            "version": 1,
            "kibanaSavedObjectMeta": {"searchSourceJSON": source()},
        },
        "references": index_ref(),
        "migrationVersion": {"visualization": "7.10.0"},
    }


def metric(label: str, agg: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "metric",
        "aggs": [{"id": "1", "enabled": True, "schema": "metric", **agg}],
        "params": {
            "addTooltip": True,
            "addLegend": False,
            "type": "metric",
            "metric": {
                "percentageMode": False,
                "useRanges": False,
                "colorSchema": "Green to Red",
                "metricColorMode": "None",
                "colorsRange": [{"from": 0, "to": 1000000}],
                "labels": {"show": True},
                "invertColors": False,
                "style": {
                    "bgFill": "#000",
                    "bgColor": False,
                    "labelColor": False,
                    "subText": label,
                    "fontSize": 48,
                },
            },
        },
    }


def bar(bucket: dict[str, Any], metric_agg: dict[str, Any], y_title: str) -> dict[str, Any]:
    return {
        "type": "histogram",
        "aggs": [
            {"id": "1", "enabled": True, "schema": "metric", **metric_agg},
            {"id": "2", "enabled": True, "schema": "segment", **bucket},
        ],
        "params": {
            "type": "histogram",
            "addTooltip": True,
            "addLegend": False,
            "legendPosition": "right",
            "times": [],
            "addTimeMarker": False,
            "grid": {"categoryLines": False},
            "categoryAxes": [
                {
                    "id": "CategoryAxis-1",
                    "type": "category",
                    "position": "bottom",
                    "show": True,
                    "scale": {"type": "linear"},
                    "labels": {"show": True, "filter": True, "truncate": 100},
                    "title": {},
                }
            ],
            "valueAxes": [
                {
                    "id": "ValueAxis-1",
                    "name": "LeftAxis-1",
                    "type": "value",
                    "position": "left",
                    "show": True,
                    "scale": {"type": "linear", "mode": "normal"},
                    "labels": {"show": True, "rotate": 0, "filter": False, "truncate": 100},
                    "title": {"text": y_title},
                }
            ],
            "seriesParams": [
                {
                    "show": True,
                    "type": "histogram",
                    "mode": "stacked",
                    "data": {"label": y_title, "id": "1"},
                    "valueAxis": "ValueAxis-1",
                    "drawLinesBetweenPoints": True,
                    "showCircles": True,
                }
            ],
        },
    }


UNIQUE_PATIENTS = {
    "type": "cardinality",
    "params": {"field": "patient_pseudonym", "customLabel": "Patients"},
}

VISUALIZATIONS = [
    vis("scoutoss-vis-patient-count", "Patients", metric("unique patients", UNIQUE_PATIENTS)),
    vis(
        "scoutoss-vis-study-count",
        "Studies",
        metric("imaging studies", {"type": "count", "params": {"customLabel": "Studies"}}),
    ),
    vis(
        "scoutoss-vis-modality",
        "Studies by modality",
        bar(
            {
                "type": "terms",
                "params": {"field": "modalities", "size": 12, "order": "desc", "orderBy": "1"},
            },
            {"type": "count", "params": {}},
            "Studies",
        ),
    ),
    vis(
        "scoutoss-vis-sex",
        "Patients by sex",
        {
            "type": "pie",
            "aggs": [
                {"id": "1", "enabled": True, "schema": "metric", **UNIQUE_PATIENTS},
                {
                    "id": "2",
                    "enabled": True,
                    "schema": "segment",
                    "type": "terms",
                    "params": {"field": "patient_sex", "size": 5, "order": "desc", "orderBy": "1"},
                },
            ],
            "params": {
                "type": "pie",
                "addTooltip": True,
                "addLegend": True,
                "legendPosition": "right",
                "isDonut": True,
                "labels": {"show": True, "values": True, "last_level": True, "truncate": 100},
            },
        },
    ),
    vis(
        "scoutoss-vis-age",
        "Age at study (years; >89 shown as 90)",
        bar(
            {
                "type": "histogram",
                "params": {
                    "field": "patient_age",
                    "interval": 10,
                    "min_doc_count": False,
                    "extended_bounds": {"min": 0, "max": 100},
                },
            },
            UNIQUE_PATIENTS,
            "Patients",
        ),
    ),
    vis(
        "scoutoss-vis-bodypart",
        "Body parts",
        {
            "type": "tagcloud",
            "aggs": [
                {"id": "1", "enabled": True, "schema": "metric", "type": "count", "params": {}},
                {
                    "id": "2",
                    "enabled": True,
                    "schema": "segment",
                    "type": "terms",
                    "params": {"field": "body_parts", "size": 25, "order": "desc", "orderBy": "1"},
                },
            ],
            "params": {
                "scale": "linear",
                "orientation": "single",
                "minFontSize": 18,
                "maxFontSize": 64,
                "showLabel": True,
            },
        },
    ),
]

SAVED_SEARCHES = [
    ("scoutoss-search-all", "All studies", ""),
    ("scoutoss-search-ct-chest", "CT chest studies", "modalities:CT and body_parts:CHEST"),
]

SAVED_QUERIES = [
    ("scoutoss-query-ct-chest", "CT chest", "modalities:CT and body_parts:CHEST"),
    ("scoutoss-query-under-85", "Age under 85", "patient_age < 85"),
    ("scoutoss-query-mr", "MR studies", "modalities:MR"),
    ("scoutoss-query-female", "Female patients", "patient_sex:F"),
]


def objects() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pid, title, time_field in INDEX_PATTERNS:
        attrs: dict[str, Any] = {"title": title}
        if time_field:
            attrs["timeFieldName"] = time_field
        out.append(
            {
                "id": pid,
                "type": "index-pattern",
                "attributes": attrs,
                "references": [],
                "migrationVersion": {"index-pattern": "7.6.0"},
            }
        )
    out.extend(VISUALIZATIONS)
    for sid, title, query in SAVED_SEARCHES:
        out.append(
            {
                "id": sid,
                "type": "search",
                "attributes": {
                    "title": title,
                    "description": "",
                    "hits": 0,
                    "columns": [
                        "patient_pseudonym",
                        "study_date",
                        "modalities",
                        "body_parts",
                        "patient_age",
                        "patient_sex",
                        "study_description",
                    ],
                    "sort": [["study_date", "desc"]],
                    "version": 1,
                    "kibanaSavedObjectMeta": {"searchSourceJSON": source(query=query)},
                },
                "references": index_ref(),
                "migrationVersion": {"search": "7.9.3"},
            }
        )
    for qid, title, query in SAVED_QUERIES:
        out.append(
            {
                "id": qid,
                "type": "query",
                "attributes": {
                    "title": title,
                    "description": f"KQL: {query}",
                    "query": {"query": query, "language": "kuery"},
                },
                "references": [],
            }
        )
    layout = [  # (vis id, x, y, w, h)
        ("scoutoss-vis-patient-count", 0, 0, 12, 7),
        ("scoutoss-vis-study-count", 12, 0, 12, 7),
        ("scoutoss-vis-sex", 24, 0, 24, 14),
        ("scoutoss-vis-modality", 0, 7, 24, 14),
        ("scoutoss-vis-age", 24, 14, 24, 14),
        ("scoutoss-vis-bodypart", 0, 21, 24, 14),
        ("scoutoss-search-all", 0, 35, 48, 16),
    ]
    panels, refs = [], []
    for i, (oid, x, y, w, h) in enumerate(layout):
        panels.append(
            {
                "panelIndex": str(i + 1),
                "gridData": {"x": x, "y": y, "w": w, "h": h, "i": str(i + 1)},
                "version": VERSION,
                "panelRefName": f"panel_{i}",
                "embeddableConfig": {},
            }
        )
        refs.append(
            {
                "name": f"panel_{i}",
                "type": "search" if oid.startswith("scoutoss-search") else "visualization",
                "id": oid,
            }
        )
    out.append(
        {
            "id": "scoutoss-overview",
            "type": "dashboard",
            "attributes": {
                "title": "ScoutOSS — Cohort overview",
                "description": "Patient and study counts, modality, sex, age and body-part distributions "
                "over de-identified imaging metadata.",
                "panelsJSON": json.dumps(panels),
                "optionsJSON": json.dumps({"hidePanelTitles": False, "useMargins": True}),
                "version": 1,
                "timeRestore": True,
                "timeFrom": "now-15y",
                "timeTo": "now",
                "refreshInterval": {"pause": False, "value": 30000},
                "kibanaSavedObjectMeta": {"searchSourceJSON": source(index_ref=False)},
            },
            "references": refs,
            "migrationVersion": {"dashboard": "7.9.3"},
        }
    )
    return out


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(o, sort_keys=True) + "\n" for o in objects()))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
