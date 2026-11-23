"""Auto-run: launch pipelines on a study when it arrives, without anyone asking.

A study has arrived once XNAT has archived its session (XNAT does that when no more images have
come in for the prearchive quiet period). This job lists the archived sessions, and for every
session it has not handled yet launches each configured pipeline through the XNAT Container
Service. Results then follow the normal path (assessors in XNAT, ``AI_DICOM`` objects out through
the DICOM export).

* ``AUTO_RUN_PIPELINES`` names the session-level pipelines (wrapper names), comma-separated; a
  pipeline can be limited to modalities: ``scoutoss-qc-session,lung-nodules:CT+PT``.
* A pipeline runs once per session. What was launched is recorded in Postgres, so a restart or a
  second cycle never launches it again; re-running is a deliberate act (run it on a cohort).
* Sessions already in XNAT when a pipeline is first configured are not run
  (``AUTO_RUN_EXISTING=run`` changes that), so switching this on does not start thousands of jobs.
* A pipeline that is not enabled in the session's project is not run there.
* Launches are paced like cohort launches, and each one is written to the audit trail first; if
  the trail is unavailable nothing is launched.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import structlog

from scoutoss_common.audit import AuditError, AuditLogger
from scoutoss_common.xnat_pipelines import PipelineError, XnatPipelines

log = structlog.get_logger(__name__)

AUDIT_USER = "service:auto-run"
LAUNCHED, SKIPPED, ERROR = "launched", "skipped", "error"

DDL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'indexer') THEN
        CREATE SCHEMA indexer;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS indexer.auto_run_pipeline (
    pipeline   text PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS indexer.auto_run (
    experiment_id text NOT NULL,
    pipeline      text NOT NULL,
    status        text NOT NULL CHECK (status IN ('launched', 'skipped', 'error')),
    attempts      integer NOT NULL DEFAULT 1,
    detail        text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (experiment_id, pipeline)
);
"""


@dataclass(frozen=True)
class Rule:
    pipeline: str
    modalities: frozenset[str] = frozenset()  # empty: every modality

    def applies_to(self, session: dict[str, Any]) -> bool:
        return not self.modalities or modality(session) in self.modalities


def modality(session: dict[str, Any]) -> str:
    """``xnat:ctSessionData`` -> ``CT``."""
    xsi = str(session.get("xsiType", ""))
    return xsi.rpartition(":")[2].removesuffix("SessionData").upper()


def parse_rules(text: str) -> list[Rule]:
    """``"a, b:CT+MR"`` -> rules; a pipeline named twice keeps its first rule. ``"none"``
    (or nothing) means no pipeline: the job then idles."""
    rules: dict[str, Rule] = {}
    for part in text.split(","):
        name, _, mods = part.strip().partition(":")
        name = name.strip()
        if not name or name.lower() == "none":
            continue
        wanted = frozenset(m.strip().upper() for m in mods.split("+") if m.strip())
        rules.setdefault(name, Rule(name, wanted))
    return list(rules.values())


@dataclass(frozen=True)
class Entry:
    status: str
    attempts: int
    detail: str | None = None


class AutoRunState(Protocol):
    def started(self, pipeline: str) -> bool: ...

    def start(self, pipeline: str, existing: list[str]) -> None:
        """Remember that ``pipeline`` is now auto-run; ``existing`` sessions are skipped."""

    def entries(self, pipeline: str) -> dict[str, Entry]: ...

    def mark(self, experiment_id: str, pipeline: str, status: str, detail: str | None) -> None: ...


class MemoryState:
    def __init__(self) -> None:
        self.pipelines: set[str] = set()
        self.rows: dict[tuple[str, str], Entry] = {}

    def started(self, pipeline: str) -> bool:
        return pipeline in self.pipelines

    def start(self, pipeline: str, existing: list[str]) -> None:
        self.pipelines.add(pipeline)
        for exp in existing:
            self.rows.setdefault((exp, pipeline), Entry(SKIPPED, 1, "already in XNAT"))

    def entries(self, pipeline: str) -> dict[str, Entry]:
        return {exp: row for (exp, name), row in self.rows.items() if name == pipeline}

    def mark(self, experiment_id: str, pipeline: str, status: str, detail: str | None) -> None:
        prev = self.rows.get((experiment_id, pipeline))
        attempts = (prev.attempts if prev else 0) + 1
        self.rows[(experiment_id, pipeline)] = Entry(status, attempts, detail)


class PostgresState:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    def migrate(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(DDL)

    def started(self, pipeline: str) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM indexer.auto_run_pipeline WHERE pipeline = %s", (pipeline,)
            ).fetchone()
        return row is not None

    def start(self, pipeline: str, existing: list[str]) -> None:
        with self.pool.connection() as conn, conn.transaction():
            for exp in existing:
                conn.execute(
                    "INSERT INTO indexer.auto_run (experiment_id, pipeline, status, detail) "
                    "VALUES (%s, %s, 'skipped', 'already in XNAT') ON CONFLICT DO NOTHING",
                    (exp, pipeline),
                )
            conn.execute(
                "INSERT INTO indexer.auto_run_pipeline (pipeline) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (pipeline,),
            )

    def entries(self, pipeline: str) -> dict[str, Entry]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT experiment_id, status, attempts, detail FROM indexer.auto_run "
                "WHERE pipeline = %s",
                (pipeline,),
            ).fetchall()
        return {r[0]: Entry(r[1], int(r[2]), r[3]) for r in rows}

    def mark(self, experiment_id: str, pipeline: str, status: str, detail: str | None) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO indexer.auto_run (experiment_id, pipeline, status, detail)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (experiment_id, pipeline) DO UPDATE SET
                    status = EXCLUDED.status,
                    attempts = indexer.auto_run.attempts + 1,
                    detail = EXCLUDED.detail,
                    updated_at = now()
                """,
                (experiment_id, pipeline, status, detail),
            )


@dataclass
class RunResult:
    launched: int = 0
    skipped: int = 0
    failed: int = 0


def auto_run_once(
    xnat: XnatPipelines,
    state: AutoRunState,
    audit: AuditLogger,
    rules: list[Rule],
    *,
    run_existing: bool = False,
    launch_interval: float = 0.25,
    max_attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    """Launch every configured pipeline on each session that has not been handled yet. An
    unreachable XNAT raises (the cycle is retried and no attempt is used up)."""
    result = RunResult()
    sessions = xnat.sessions()
    wrappers: dict[tuple[str, str], dict[str, Any] | None] = {}
    for rule in rules:
        if not state.started(rule.pipeline):
            existing = [] if run_existing else [str(s.get("ID")) for s in sessions]
            state.start(rule.pipeline, existing)
            log.info("auto-run enabled", pipeline=rule.pipeline, existing_skipped=len(existing))
        known = state.entries(rule.pipeline)
        for session in sessions:
            exp_id, project = str(session.get("ID")), str(session.get("project") or "")
            entry = known.get(exp_id)
            if entry is not None and not (entry.status == ERROR and entry.attempts < max_attempts):
                continue
            if not rule.applies_to(session):
                state.mark(exp_id, rule.pipeline, SKIPPED, "modality not selected")
                result.skipped += 1
                continue
            if (project, rule.pipeline) not in wrappers:
                found = [w for w in xnat.wrappers(project) if w["name"] == rule.pipeline]
                wrappers[(project, rule.pipeline)] = found[0] if found else None
            wrapper = wrappers[(project, rule.pipeline)]
            if wrapper is None:
                state.mark(exp_id, rule.pipeline, SKIPPED, "pipeline not enabled in project")
                result.skipped += 1
                log.warning("auto-run skipped", pipeline=rule.pipeline, project=project)
                continue
            params = {"pipeline": rule.pipeline, "trigger": "arrival"}
            try:
                audit.record(
                    user=AUDIT_USER,
                    action="pipeline_launch",
                    resource=exp_id,
                    params=params,
                    outcome="attempted",
                )
            except AuditError:
                log.error("auto-run skipped: audit trail unavailable", experiment=exp_id)
                continue
            if result.launched + result.failed and launch_interval > 0:
                sleep(launch_interval)  # see PIPELINE_LAUNCH_INTERVAL_SECONDS in the runbook
            try:
                xnat.launch(project, wrapper, exp_id)
            except PipelineError as exc:
                state.mark(exp_id, rule.pipeline, ERROR, str(exc)[:200])
                result.failed += 1
                log.warning("auto-run launch failed", experiment=exp_id, pipeline=rule.pipeline)
                audit.record(
                    user=AUDIT_USER,
                    action="pipeline_launch",
                    resource=exp_id,
                    params=params,
                    outcome="failure",
                )
                continue
            state.mark(exp_id, rule.pipeline, LAUNCHED, None)
            result.launched += 1
    if result.launched or result.skipped or result.failed:
        log.info(
            "auto-run cycle",
            launched=result.launched,
            skipped=result.skipped,
            failed=result.failed,
        )
    return result


def status(state: AutoRunState, rules: list[Rule]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for rule in rules:
        entries = state.entries(rule.pipeline)
        counts = dict.fromkeys((LAUNCHED, SKIPPED, ERROR), 0)
        for entry in entries.values():
            counts[entry.status] += 1
        report[rule.pipeline] = {
            "enabled": state.started(rule.pipeline),
            **counts,
            "sessions_launched": sorted(e for e, row in entries.items() if row.status == LAUNCHED),
        }
    return report


def run_forever(
    xnat: XnatPipelines,
    state: AutoRunState,
    audit: AuditLogger,
    rules: list[Rule],
    *,
    interval: float,
    run_existing: bool = False,
    launch_interval: float = 0.25,
    max_attempts: int = 3,
    heartbeat: Path | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Check for new sessions every ``interval`` seconds. A failed cycle is logged and retried;
    it never exits."""
    stop = stop or threading.Event()
    log.info("auto-run started", pipelines=[r.pipeline for r in rules], interval_seconds=interval)
    while not stop.is_set():
        try:
            auto_run_once(
                xnat,
                state,
                audit,
                rules,
                run_existing=run_existing,
                launch_interval=launch_interval,
                max_attempts=max_attempts,
            )
            if heartbeat is not None:
                heartbeat.touch()
        except (httpx.HTTPError, AuditError, KeyError, ValueError, OSError) as exc:
            log.warning("auto-run cycle failed", error=type(exc).__name__, detail=str(exc)[:200])
        stop.wait(interval)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entry point
    from psycopg_pool import ConnectionPool

    from scoutoss_common.logging import configure_logging
    from scoutoss_indexer.settings import get_settings
    from scoutoss_indexer.store import make_client

    parser = argparse.ArgumentParser(prog="scoutoss-indexer auto-run")
    parser.add_argument("--status", action="store_true", help="print what was launched and exit")
    args = parser.parse_args(argv)
    configure_logging("auto-run")
    settings = get_settings()
    rules = parse_rules(os.environ.get("AUTO_RUN_PIPELINES", ""))
    state = PostgresState(ConnectionPool(settings.database_url, min_size=1, max_size=2, open=True))
    state.migrate()
    if args.status:
        print(json.dumps(status(state, rules)))
        return 0
    if not rules:
        log.warning("AUTO_RUN_PIPELINES is empty: nothing will be launched")
    xnat = XnatPipelines(settings.xnat_url, settings.xnat_user, settings.xnat_secret())
    audit = AuditLogger(
        make_client(
            settings.opensearch_url,
            settings.opensearch_user,
            settings.opensearch_password,
            settings.opensearch_verify_certs,
        ),
        service="auto-run",
    )
    run_forever(
        xnat,
        state,
        audit,
        rules,
        interval=float(os.environ.get("AUTO_RUN_SECONDS", "15")),
        run_existing=os.environ.get("AUTO_RUN_EXISTING", "skip").lower() == "run",
        launch_interval=float(os.environ.get("PIPELINE_LAUNCH_INTERVAL_SECONDS", "0.25")),
        max_attempts=int(os.environ.get("AUTO_RUN_MAX_ATTEMPTS", "3")),
        heartbeat=Path(os.environ.get("AUTO_RUN_HEARTBEAT", "/tmp/auto-run.ok")),  # noqa: S108
    )
    return 0
