"""OpenSearch writes: idempotent bulk indexing, study/patient rollups and the DLQ.

Idempotency: every document's ``_id`` is derived from its UID (series, study) or the
patient pseudonym, and rollups are *recomputed* from the series index rather than
incremented — reprocessing the same study any number of times yields identical docs.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from opensearchpy import OpenSearch, helpers
from prometheus_client import Counter

from scoutoss_common.audit import AUDIT_INDEX, AUDIT_MAPPING
from scoutoss_common.catalog import (
    CLINICAL_INDEX,
    DLQ_INDEX,
    INDEX_DEFINITIONS,
    PATIENTS_INDEX,
    SERIES_INDEX,
    STUDIES_INDEX,
)
from scoutoss_common.phi import redact_text
from scoutoss_indexer.extract import SeriesRecord, series_doc_id, study_doc_id

log = structlog.get_logger(__name__)

DOCS_INDEXED = Counter(
    "indexer_documents_indexed_total", "Documents written to OpenSearch", ["index"]
)
DLQ_TOTAL = Counter("indexer_dlq_total", "Items routed to the dead-letter queue", ["stage"])


def make_client(
    url: str, user: str | None = None, password: str | None = None, verify: bool = True
) -> OpenSearch:
    auth = (user, password) if user and password else None
    return OpenSearch(
        hosts=[url],
        http_auth=auth,
        verify_certs=verify,
        ssl_show_warn=False,
        timeout=30,
        max_retries=3,
        retry_on_timeout=True,
    )


class IndexStore:
    def __init__(self, client: OpenSearch, *, xnat_public_url: str | None = None) -> None:
        self.client = client
        self.xnat_public_url = (xnat_public_url or "").rstrip("/")

    # ------------------------------------------------------------ schema
    def ensure_indices(self) -> list[str]:
        created: list[str] = []
        definitions = {**INDEX_DEFINITIONS, AUDIT_INDEX: AUDIT_MAPPING}
        for name, body in definitions.items():
            if not self.client.indices.exists(index=name):
                try:
                    self.client.indices.create(index=name, body=body)
                    created.append(name)
                except Exception as exc:  # another replica won the race
                    if "resource_already_exists_exception" not in str(exc):
                        raise
            else:
                # Mappings evolve additively: new fields are added to existing indices.
                # (Type changes need a reindex — see docs/runbook.md.)
                props = body.get("mappings", {}).get("properties", {})
                if props:
                    self.client.indices.put_mapping(index=name, body={"properties": props})
        if created:
            log.info("indices created", indices=created)
        return created

    # ------------------------------------------------------------ series
    def index_series(self, records: Sequence[SeriesRecord]) -> tuple[int, list[dict[str, Any]]]:
        """Bulk index series docs. Returns (ok_count, failed items)."""
        if not records:
            return 0, []
        actions = (
            {
                "_op_type": "index",
                "_index": SERIES_INDEX,
                "_id": series_doc_id(r.series_uid),
                "_source": r.to_doc(),
            }
            for r in records
        )
        ok, errors = helpers.bulk(
            self.client, actions, raise_on_error=False, raise_on_exception=False, refresh=True
        )
        failed = [e for e in errors if isinstance(e, dict)]
        DOCS_INDEXED.labels(SERIES_INDEX).inc(ok)
        return int(ok), failed

    def _series_for_studies(self, study_uids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        by_study: dict[str, list[dict[str, Any]]] = defaultdict(list)
        uids = sorted(set(study_uids))
        for i in range(0, len(uids), 500):
            chunk = uids[i : i + 500]
            for hit in helpers.scan(
                self.client,
                index=SERIES_INDEX,
                query={"query": {"terms": {"study_uid": chunk}}},
                size=500,
                preserve_order=False,
            ):
                src = hit["_source"]
                by_study[src["study_uid"]].append(src)
        return by_study

    # ------------------------------------------------------------ studies
    def rollup_studies(self, study_uids: Iterable[str]) -> set[str]:
        """Recompute study docs from their series; returns affected pseudonyms."""
        by_study = self._series_for_studies(study_uids)
        now = datetime.now(UTC).isoformat()
        actions = []
        pseudonyms: set[str] = set()
        for uid, series in by_study.items():
            doc = build_study_doc(uid, series, self.xnat_public_url)
            doc["indexed_at"] = now
            pseudonyms.add(doc["patient_pseudonym"])
            actions.append(
                {
                    "_op_type": "index",
                    "_index": STUDIES_INDEX,
                    "_id": study_doc_id(uid),
                    "_source": doc,
                }
            )
        if actions:
            ok, _ = helpers.bulk(self.client, actions, refresh=True)
            DOCS_INDEXED.labels(STUDIES_INDEX).inc(int(ok))
        return pseudonyms

    # ------------------------------------------------------------ patients
    def rollup_patients(self, pseudonyms: Iterable[str]) -> int:
        ids = sorted(set(pseudonyms))
        if not ids:
            return 0
        studies: dict[str, list[dict[str, Any]]] = defaultdict(list)
        clinical: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            query = {"query": {"terms": {"patient_pseudonym": chunk}}}
            for hit in helpers.scan(self.client, index=STUDIES_INDEX, query=query, size=500):
                studies[hit["_source"]["patient_pseudonym"]].append(hit["_source"])
            if self.client.indices.exists(index=CLINICAL_INDEX):
                for hit in helpers.scan(self.client, index=CLINICAL_INDEX, query=query, size=500):
                    clinical[hit["_source"]["patient_pseudonym"]].append(hit["_source"])
        now = datetime.now(UTC).isoformat()
        actions = []
        for pid in ids:
            if not studies.get(pid) and not clinical.get(pid):
                continue
            doc = build_patient_doc(pid, studies.get(pid, []), clinical.get(pid, []))
            doc["indexed_at"] = now
            actions.append(
                {"_op_type": "index", "_index": PATIENTS_INDEX, "_id": pid, "_source": doc}
            )
        if actions:
            ok, _ = helpers.bulk(self.client, actions, refresh=True)
            DOCS_INDEXED.labels(PATIENTS_INDEX).inc(int(ok))
        return len(actions)

    # ------------------------------------------------------------ DLQ
    def dead_letter(self, *, ref: str, source: str, stage: str, error: BaseException | str) -> str:
        """Record an item that could not be processed. Idempotent per (ref, stage)."""
        safe_ref = redact_text(ref, detect_names=False)
        doc_id = hashlib.sha256(f"{source}|{ref}|{stage}".encode()).hexdigest()
        message = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        doc = {
            "ref": safe_ref,
            "source": source,
            "stage": stage,
            "error_type": error if isinstance(error, str) else type(error).__name__,
            "error": redact_text(message)[:500],
            "attempts": 1,
            "ts": datetime.now(UTC).isoformat(),
        }
        try:
            self.client.update(
                index=DLQ_INDEX,
                id=doc_id,
                body={
                    "script": {
                        "source": "ctx._source.attempts += 1; ctx._source.ts = params.ts",
                        "params": {"ts": doc["ts"]},
                    },
                    "upsert": doc,
                },
                refresh=True,
            )
        except Exception as exc:  # the DLQ itself must never take the worker down
            log.error("dlq write failed", stage=stage, error=type(exc).__name__)
        DLQ_TOTAL.labels(stage).inc()
        log.warning("dead-lettered", source=source, stage=stage, error_type=doc["error_type"])
        return doc_id

    def count(self, index: str) -> int:
        if not self.client.indices.exists(index=index):
            return 0
        self.client.indices.refresh(index=index)
        return int(self.client.count(index=index)["count"])


def _uniq(values: Iterable[Any]) -> list[Any]:
    seen: list[Any] = []
    for v in values:
        if v is not None and v not in seen:
            seen.append(v)
    return seen


def build_study_doc(
    study_uid: str, series: list[dict[str, Any]], xnat_public_url: str = ""
) -> dict[str, Any]:
    series = sorted(series, key=lambda s: (s.get("series_number") or 0, s["series_uid"]))
    head = series[0]
    modalities = _uniq(s.get("modality") for s in series)
    experiment = next(
        (s.get("xnat_experiment_id") for s in series if s.get("xnat_experiment_id")), None
    )
    project = next((s.get("project") for s in series if s.get("project")), None)
    subject = next((s.get("xnat_subject_id") for s in series if s.get("xnat_subject_id")), None)
    viewer = None
    if xnat_public_url and experiment and project:
        viewer = (
            f"{xnat_public_url}/VIEWER/?subjectId={subject or ''}&projectId={project}"
            f"&experimentId={experiment}&experimentLabel="
            f"{head.get('xnat_session_label') or ''}"
        )
    return {
        "study_uid": study_uid,
        "patient_pseudonym": head["patient_pseudonym"],
        "project": project,
        "study_date": head.get("study_date"),
        "study_year": head.get("study_year"),
        "study_description": next(
            (s.get("study_description") for s in series if s.get("study_description")), None
        ),
        "modalities": modalities,
        "primary_modality": modalities[0] if modalities else None,
        "body_parts": _uniq(s.get("body_part") for s in series),
        "series_count": len(series),
        "instance_count": sum(int(s.get("instance_count") or 0) for s in series),
        "patient_sex": next((s.get("patient_sex") for s in series if s.get("patient_sex")), None),
        "patient_age": next(
            (s.get("patient_age") for s in series if s.get("patient_age") is not None), None
        ),
        "manufacturers": _uniq(s.get("manufacturer") for s in series),
        "institution": next((s.get("institution") for s in series if s.get("institution")), None),
        "xnat_experiment_id": experiment,
        "xnat_session_label": head.get("xnat_session_label"),
        "xnat_subject_id": subject,
        "viewer_url": viewer,
    }


def build_patient_doc(
    pseudonym: str, studies: list[dict[str, Any]], clinical: list[dict[str, Any]]
) -> dict[str, Any]:
    studies = sorted(studies, key=lambda s: (s.get("study_date") or "", s["study_uid"]))
    dates = [s["study_date"] for s in studies if s.get("study_date")]
    latest_age = next(
        (s.get("patient_age") for s in reversed(studies) if s.get("patient_age") is not None),
        None,
    )
    if latest_age is None:
        latest_age = next(
            (c.get("patient_age") for c in clinical if c.get("patient_age") is not None), None
        )
    sex = next((s.get("patient_sex") for s in studies if s.get("patient_sex")), None) or next(
        (c.get("patient_sex") for c in clinical if c.get("patient_sex")), None
    )
    return {
        "patient_pseudonym": pseudonym,
        "patient_sex": sex,
        "age_latest": latest_age,
        "study_count": len(studies),
        "series_count": sum(int(s.get("series_count") or 0) for s in studies),
        "modalities": _uniq(m for s in studies for m in s.get("modalities") or []),
        "body_parts": _uniq(b for s in studies for b in s.get("body_parts") or []),
        "projects": _uniq(
            [s.get("project") for s in studies] + [c.get("project") for c in clinical]
        ),
        "first_study_date": dates[0] if dates else None,
        "last_study_date": dates[-1] if dates else None,
        "diagnoses": sorted(
            {c["diagnosis_category"] for c in clinical if c.get("diagnosis_category")}
        ),
        "diagnosis_codes": sorted(
            {c["diagnosis_code"] for c in clinical if c.get("diagnosis_code")}
        ),
    }
