"""The polling worker. Its loop catches everything: a bad file is dead-lettered, a bad
session is retried (up to ``max_attempts``) and an unreachable XNAT/OpenSearch just
delays the next cycle. Nothing in here may end the loop except ``stop()``."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import structlog
from opentelemetry import trace
from prometheus_client import Counter, Gauge

from scoutoss_indexer.cursor import CursorStore
from scoutoss_indexer.pipeline import BatchResult, Pipeline
from scoutoss_indexer.sources.folder import FolderSource
from scoutoss_indexer.sources.xnat import XnatSource

log = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)

POLL_CYCLES = Counter("indexer_poll_cycles_total", "Worker poll cycles", ["source", "outcome"])
LAST_SUCCESS = Gauge("indexer_last_success_timestamp_seconds", "Last successful poll (unix time)")
SESSIONS_INDEXED = Counter("indexer_sessions_indexed_total", "XNAT sessions indexed")


@dataclass
class WorkerState:
    cycles: int = 0
    last_ok: float | None = None
    last_error: str | None = None
    totals: BatchResult = field(default_factory=BatchResult)


class Worker:
    def __init__(
        self,
        pipeline: Pipeline,
        source: XnatSource | FolderSource,
        *,
        cursor: CursorStore | None = None,
        poll_seconds: float = 10.0,
        max_attempts: int = 5,
        batch_size: int = 200,
    ) -> None:
        self.pipeline = pipeline
        self.source = source
        self.cursor = cursor
        self.poll_seconds = poll_seconds
        self.max_attempts = max_attempts
        self.batch_size = batch_size
        self.state = WorkerState()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._only: set[str] | None = None
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()  # one poll cycle at a time
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ control
    def trigger(self, experiment_ids: set[str] | None = None) -> None:
        with self._lock:
            if experiment_ids:
                self._only = (self._only or set()) | experiment_ids
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="indexer-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        log.info("worker started", source=self.source.name, poll_seconds=self.poll_seconds)
        failures = 0
        while not self._stop.is_set():
            ok = self.run_once_safe()
            failures = 0 if ok else min(failures + 1, 6)
            delay = self.poll_seconds * (2**failures if failures else 1)
            self._wake.wait(timeout=min(delay, 300))
            self._wake.clear()
        log.info("worker stopped")

    def run_once_safe(self) -> bool:
        try:
            self.run_once()
        except Exception as exc:  # never let the loop die
            self.state.last_error = type(exc).__name__
            POLL_CYCLES.labels(self.source.name, "error").inc()
            log.error("poll cycle failed", error=type(exc).__name__, detail=str(exc)[:200])
            if self.cursor is not None:
                try:
                    self.cursor.touch_poll(self.source.name, ok=False)
                except Exception:
                    log.warning("cursor unavailable")
            return False
        return True

    def run_once(self) -> BatchResult:
        with self._run_lock:
            return self._cycle()

    def _cycle(self) -> BatchResult:
        with tracer.start_as_current_span("indexer.poll", attributes={"source": self.source.name}):
            result = (
                self._run_xnat(self.source)
                if isinstance(self.source, XnatSource)
                else self._run_folder(self.source)
            )
        self.state.cycles += 1
        self.state.last_ok = time.time()
        self.state.last_error = None
        self.state.totals.merge(result)
        LAST_SUCCESS.set(self.state.last_ok)
        POLL_CYCLES.labels(self.source.name, "ok").inc()
        if self.cursor is not None:
            self.cursor.touch_poll(self.source.name, ok=True)
        return result

    def _run_folder(self, source: FolderSource) -> BatchResult:
        # One pass over all new files: instances of a series must be counted together.
        # Headers are read lazily one at a time, so memory is per-series, not per-file.
        return self.pipeline.process(source.pending())

    def _run_xnat(self, source: XnatSource) -> BatchResult:
        assert self.cursor is not None
        with self._lock:
            only, self._only = self._only, None
        total = BatchResult()
        for row in source.pending_sessions(only):
            exp_id = str(row.get("ID"))
            project = row.get("project")
            try:
                work = source.build(row)
                result = self.pipeline.process(work.items)
            except Exception as exc:
                log.warning("session failed", experiment_id=exp_id, error=type(exc).__name__)
                self.cursor.mark(
                    exp_id,
                    project=project,
                    change_token=str(row.get("last_modified") or row.get("insert_date") or "0"),
                    status="error",
                    error=type(exc).__name__,
                )
                if self._attempts(exp_id) >= self.max_attempts:
                    self.pipeline.store.dead_letter(
                        ref=f"xnat:{exp_id}", source="xnat", stage="fetch", error=exc
                    )
                continue
            status = "indexed" if result.indexed or not work.items else "error"
            self.cursor.mark(
                exp_id,
                project=project,
                change_token=work.change_token,
                status=status,
                series_indexed=result.indexed,
            )
            if status == "indexed":
                SESSIONS_INDEXED.inc()
            total.merge(result)
        return total

    def _attempts(self, exp_id: str) -> int:
        assert self.cursor is not None
        entry = self.cursor.get_all().get(exp_id)
        return entry.attempts if entry else 0
