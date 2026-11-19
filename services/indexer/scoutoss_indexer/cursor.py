"""Postgres cursor table: which XNAT sessions have been indexed, and at which version."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from psycopg_pool import ConnectionPool

DDL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'indexer') THEN
        CREATE SCHEMA indexer;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS indexer.xnat_cursor (
    experiment_id  text PRIMARY KEY,
    project        text,
    change_token   text NOT NULL,
    status         text NOT NULL CHECK (status IN ('indexed', 'error', 'pending')),
    series_indexed integer NOT NULL DEFAULT 0,
    attempts       integer NOT NULL DEFAULT 0,
    last_error     text,
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS indexer.poll_state (
    source       text PRIMARY KEY,
    last_poll_at timestamptz,
    last_ok_at   timestamptz,
    cycles       bigint NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class CursorEntry:
    experiment_id: str
    change_token: str
    status: str
    attempts: int


class CursorStore(Protocol):
    def get_all(self) -> dict[str, CursorEntry]: ...

    def mark(
        self,
        experiment_id: str,
        *,
        project: str | None,
        change_token: str,
        status: str,
        series_indexed: int = 0,
        error: str | None = None,
    ) -> None: ...

    def reset(
        self, *, project: str | None = None, experiment_ids: list[str] | None = None
    ) -> int: ...

    def touch_poll(self, source: str, ok: bool) -> None: ...


def needs_processing(entry: CursorEntry | None, change_token: str, max_attempts: int) -> bool:
    if entry is None:
        return True
    if entry.change_token != change_token:
        return True
    return entry.status != "indexed" and entry.attempts < max_attempts


class MemoryCursor:
    def __init__(self) -> None:
        self.rows: dict[str, CursorEntry] = {}
        self.polls: dict[str, int] = {}
        self.projects: dict[str, str | None] = {}

    def get_all(self) -> dict[str, CursorEntry]:
        return dict(self.rows)

    def mark(
        self,
        experiment_id: str,
        *,
        project: str | None,
        change_token: str,
        status: str,
        series_indexed: int = 0,
        error: str | None = None,
    ) -> None:
        prev = self.rows.get(experiment_id)
        attempts = 0 if status == "indexed" else (prev.attempts + 1 if prev else 1)
        self.rows[experiment_id] = CursorEntry(experiment_id, change_token, status, attempts)
        self.projects[experiment_id] = project

    def reset(self, *, project: str | None = None, experiment_ids: list[str] | None = None) -> int:
        doomed = [
            k
            for k in self.rows
            if (experiment_ids is None or k in experiment_ids)
            and (project is None or self.projects.get(k) == project)
        ]
        for k in doomed:
            del self.rows[k]
        return len(doomed)

    def touch_poll(self, source: str, ok: bool) -> None:
        self.polls[source] = self.polls.get(source, 0) + 1


class PostgresCursor:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def migrate(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(DDL)

    def get_all(self) -> dict[str, CursorEntry]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT experiment_id, change_token, status, attempts FROM indexer.xnat_cursor"
            ).fetchall()
        return {r[0]: CursorEntry(r[0], r[1], r[2], int(r[3])) for r in rows}

    def mark(
        self,
        experiment_id: str,
        *,
        project: str | None,
        change_token: str,
        status: str,
        series_indexed: int = 0,
        error: str | None = None,
    ) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO indexer.xnat_cursor
                    (experiment_id, project, change_token, status, series_indexed, attempts,
                     last_error, updated_at)
                VALUES (%(id)s, %(project)s, %(token)s, %(status)s, %(n)s,
                        CASE WHEN %(status)s = 'indexed' THEN 0 ELSE 1 END, %(error)s, now())
                ON CONFLICT (experiment_id) DO UPDATE SET
                    project = EXCLUDED.project,
                    change_token = EXCLUDED.change_token,
                    status = EXCLUDED.status,
                    series_indexed = EXCLUDED.series_indexed,
                    attempts = CASE WHEN EXCLUDED.status = 'indexed' THEN 0
                                    ELSE indexer.xnat_cursor.attempts + 1 END,
                    last_error = EXCLUDED.last_error,
                    updated_at = now()
                """,
                {
                    "id": experiment_id,
                    "project": project,
                    "token": change_token,
                    "status": status,
                    "n": series_indexed,
                    "error": error,
                },
            )

    def reset(self, *, project: str | None = None, experiment_ids: list[str] | None = None) -> int:
        clauses, params = [], []
        if project is not None:
            clauses.append("project = %s")
            params.append(project)
        if experiment_ids is not None:
            clauses.append("experiment_id = ANY(%s)")
            params.append(experiment_ids)  # type: ignore[arg-type]
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.pool.connection() as conn:
            cur = conn.execute("DELETE FROM indexer.xnat_cursor" + where, params)  # noqa: S608
            return int(cur.rowcount or 0)

    def touch_poll(self, source: str, ok: bool) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO indexer.poll_state (source, last_poll_at, last_ok_at, cycles)
                VALUES (%s, now(), CASE WHEN %s THEN now() END, 1)
                ON CONFLICT (source) DO UPDATE SET
                    last_poll_at = now(),
                    last_ok_at = CASE WHEN %s THEN now() ELSE indexer.poll_state.last_ok_at END,
                    cycles = indexer.poll_state.cycles + 1
                """,
                (source, ok, ok),
            )
