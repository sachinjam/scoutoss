"""One batch of work: raw headers in, series records out, failures to the DLQ.

The pipeline never raises for a bad item. Each item runs read -> de-identify -> extract
in its own try/except; failures are dead-lettered with the stage that failed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from scoutoss_indexer.deid.deidentify import (
    DeidConfig,
    Deidentifier,
    DeidRejected,
    is_scoutoss_deidentified,
)
from scoutoss_indexer.deid.vault import Vault
from scoutoss_indexer.extract import ExtractionError, SeriesRecord, extract_series, read_header
from scoutoss_indexer.store import IndexStore

log = structlog.get_logger(__name__)


@dataclass
class WorkItem:
    """A single series header to index. ``load`` returns DICOM bytes or a Path."""

    ref: str
    load: Callable[[], bytes | Path]
    source: str = "folder"
    instance_count: int = 1
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchResult:
    indexed: int = 0
    dead_lettered: int = 0
    studies: set[str] = field(default_factory=set)
    patients: set[str] = field(default_factory=set)
    failed_refs: list[str] = field(default_factory=list)

    def merge(self, other: BatchResult) -> None:
        self.indexed += other.indexed
        self.dead_lettered += other.dead_lettered
        self.studies |= other.studies
        self.patients |= other.patients
        self.failed_refs += other.failed_refs


class Pipeline:
    def __init__(
        self,
        store: IndexStore,
        *,
        salt: bytes,
        vault: Vault,
        default_project: str = "UNASSIGNED",
    ) -> None:
        self.store = store
        self.salt = salt
        self.vault = vault
        self.default_project = default_project

    def _record(self, item: WorkItem) -> SeriesRecord:
        ds = read_header(item.load())
        if not is_scoutoss_deidentified(ds):
            # Defense in depth: never index identifiable headers.
            project = str(item.context.get("project") or self.default_project)
            deid = Deidentifier(
                DeidConfig(
                    salt=self.salt, project=project, offset_days=self.vault.project_offset(project)
                )
            )
            result = deid.apply(ds)
            self.vault.record_subject(result.pseudonym, result.original_patient_id, project)
        return extract_series(
            ds,
            salt=self.salt,
            instance_count=item.instance_count,
            context={"source": item.source, **item.context},
        )

    def process(self, items: Iterable[WorkItem]) -> BatchResult:
        result = BatchResult()
        records: dict[str, SeriesRecord] = {}
        refs: dict[str, str] = {}
        for item in items:
            try:
                record = self._record(item)
            except ExtractionError as exc:
                self._dlq(item, "extract", exc, result)
                continue
            except DeidRejected as exc:
                self._dlq(item, "deid", exc, result)
                continue
            except Exception as exc:
                self._dlq(item, "read", exc, result)
                continue
            # Several instances of one series collapse to one record; keep the count.
            existing = records.get(record.series_uid)
            if existing is not None and item.source == "folder":
                existing.instance_count += record.instance_count
            else:
                records[record.series_uid] = record
                refs[record.series_uid] = item.ref

        try:
            ok, failed = self.store.index_series(list(records.values()))
        except Exception as exc:
            for uid in records:
                self.store.dead_letter(ref=refs[uid], source="index", stage="index", error=exc)
            result.dead_lettered += len(records)
            log.error("bulk index failed", error=type(exc).__name__, series=len(records))
            return result

        failed_ids = set()
        for failure in failed:
            info: dict[str, Any] = next(iter(failure.values()), {})
            doc_id = str(info.get("_id", ""))
            failed_ids.add(doc_id)
            self.store.dead_letter(
                ref=refs.get(doc_id, doc_id),
                source="index",
                stage="index",
                error=str(info.get("error", {}).get("type", "index_error")),
            )
            result.dead_lettered += 1
        result.indexed = ok
        good = [r for uid, r in records.items() if uid not in failed_ids]
        result.studies = {r.study_uid for r in good}
        pseudonyms = self.store.rollup_studies(result.studies)
        self.store.rollup_patients(pseudonyms)
        result.patients = pseudonyms
        log.info(
            "batch indexed",
            series=ok,
            studies=len(result.studies),
            patients=len(pseudonyms),
            dead_lettered=result.dead_lettered,
        )
        return result

    def _dlq(self, item: WorkItem, stage: str, exc: BaseException, result: BatchResult) -> None:
        self.store.dead_letter(ref=item.ref, source=item.source, stage=stage, error=exc)
        result.dead_lettered += 1
        result.failed_refs.append(item.ref)
