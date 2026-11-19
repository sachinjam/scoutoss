"""Server-side chart generation as Vega-Lite v5 specs (rendered by the UI with vega-embed).

Charts are chosen from the plan's ``charts`` hints (question intent) and the shape of the
result; values are aggregate counts only — never row-level data.
"""

from __future__ import annotations

from typing import Any

SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
SUPPORTED = ("modality", "sex", "age", "body_part", "year", "diagnosis", "manufacturer")
DEFAULTS = ("modality", "sex", "age")

TITLES = {
    "modality": "Studies by modality",
    "sex": "Patients by sex",
    "age": "Age at study (10-year bins; >89 shown as 90)",
    "body_part": "Studies by body part",
    "year": "Studies per year (date-shifted)",
    "diagnosis": "Patients by diagnosis (HL7)",
    "manufacturer": "Studies by scanner manufacturer",
}
SEX_LABELS = {"F": "Female", "M": "Male", "O": "Other", "U": "Unknown"}


def _values(stats: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    data = stats.get(kind) or {}
    if kind == "sex":
        return [{"category": SEX_LABELS.get(k, k), "count": v} for k, v in data.items()]
    if kind == "age":
        return [
            {"bin": int(k), "bin_end": int(k) + 10, "count": v}
            for k, v in sorted(data.items(), key=lambda kv: int(kv[0]))
        ]
    if kind == "year":
        return [{"year": str(k), "count": v} for k, v in sorted(data.items())]
    return [{"category": k, "count": v} for k, v in data.items()]


def bar_spec(kind: str, values: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "$schema": SCHEMA,
        "title": TITLES[kind],
        "data": {"values": values},
        "mark": {"type": "bar", "cornerRadiusEnd": 3, "tooltip": True},
        "encoding": {
            "y": {"field": "category", "type": "nominal", "sort": "-x", "title": None},
            "x": {
                "field": "count",
                "type": "quantitative",
                "title": "Patients" if kind == "diagnosis" else "Studies",
            },
        },
    }


def donut_spec(values: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "$schema": SCHEMA,
        "title": TITLES["sex"],
        "data": {"values": values},
        "mark": {"type": "arc", "innerRadius": 50, "tooltip": True},
        "encoding": {
            "theta": {"field": "count", "type": "quantitative"},
            "color": {"field": "category", "type": "nominal", "title": "Sex"},
        },
    }


def histogram_spec(values: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "$schema": SCHEMA,
        "title": TITLES["age"],
        "data": {"values": values},
        "mark": {"type": "bar", "tooltip": True},
        "encoding": {
            "x": {"field": "bin", "bin": {"binned": True, "step": 10}, "title": "Age (years)"},
            "x2": {"field": "bin_end"},
            "y": {"field": "count", "type": "quantitative", "title": "Studies"},
        },
    }


def line_spec(values: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "$schema": SCHEMA,
        "title": TITLES["year"],
        "data": {"values": values},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {"field": "year", "type": "ordinal", "title": "Year"},
            "y": {"field": "count", "type": "quantitative", "title": "Studies"},
        },
    }


def chart_for(kind: str, stats: dict[str, Any]) -> dict[str, Any] | None:
    if kind not in SUPPORTED:
        return None
    values = _values(stats, kind)
    if not values:
        return None
    if kind == "sex":
        spec = donut_spec(values)
    elif kind == "age":
        spec = histogram_spec(values)
    elif kind == "year":
        spec = line_spec(values)
    else:
        spec = bar_spec(kind, values)
    return {"id": kind, "type": spec["mark"]["type"], "title": TITLES[kind], "spec": spec}


def choose_charts(hints: list[str], stats: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    """Charts matching the question intent first, then sensible defaults."""
    order = [h for h in hints if h in SUPPORTED] + [d for d in DEFAULTS if d not in hints]
    charts: list[dict[str, Any]] = []
    for kind in order:
        chart = chart_for(kind, stats)
        if chart:
            charts.append(chart)
        if len(charts) >= limit:
            break
    return charts
