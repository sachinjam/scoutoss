"""Cohort persistence (Postgres ``copilot.cohorts``) and per-session conversation memory."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

DDL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'copilot') THEN
        CREATE SCHEMA copilot;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS copilot.cohorts (
    id           uuid PRIMARY KEY,
    name         text NOT NULL,
    question     text NOT NULL,
    query        jsonb NOT NULL,
    members      text[] NOT NULL,
    study_uids   text[] NOT NULL DEFAULT '{}',
    member_count integer NOT NULL,
    created_by   text NOT NULL,
    saved        boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cohorts_user_idx ON copilot.cohorts (created_by, created_at DESC);
"""


@dataclass
class Cohort:
    id: str
    name: str
    question: str
    query: dict[str, Any]
    members: list[str]
    study_uids: list[str]
    created_by: str
    saved: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def member_count(self) -> int:
        return len(self.members)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "question": self.question,
            "member_count": self.member_count,
            "study_count": len(self.study_uids),
            "saved": self.saved,
            "created_at": self.created_at,
            "query": self.query,
        }


class CohortRepo(Protocol):
    def create(self, cohort: Cohort) -> Cohort: ...

    def get(self, cohort_id: str, user: str) -> Cohort | None: ...

    def save(self, cohort_id: str, user: str, name: str) -> Cohort | None: ...

    def list(self, user: str, limit: int = 50) -> list[Cohort]: ...


def new_cohort(
    *,
    name: str,
    question: str,
    query: dict[str, Any],
    members: list[str],
    study_uids: list[str],
    user: str,
) -> Cohort:
    return Cohort(
        id=str(uuid.uuid4()),
        name=name[:200],
        question=question[:2000],
        query=query,
        members=members,
        study_uids=study_uids,
        created_by=user,
    )


class MemoryCohortRepo:
    def __init__(self) -> None:
        self.rows: dict[str, Cohort] = {}

    def create(self, cohort: Cohort) -> Cohort:
        self.rows[cohort.id] = cohort
        return cohort

    def get(self, cohort_id: str, user: str) -> Cohort | None:
        c = self.rows.get(cohort_id)
        return c if c and c.created_by == user else None

    def save(self, cohort_id: str, user: str, name: str) -> Cohort | None:
        c = self.get(cohort_id, user)
        if c:
            c.name, c.saved = name[:200], True
        return c

    def list(self, user: str, limit: int = 50) -> list[Cohort]:
        mine = [c for c in self.rows.values() if c.created_by == user]
        return sorted(mine, key=lambda c: c.created_at, reverse=True)[:limit]


class PostgresCohortRepo:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def migrate(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(DDL)

    @staticmethod
    def _row(r: dict[str, Any]) -> Cohort:
        return Cohort(
            id=str(r["id"]),
            name=r["name"],
            question=r["question"],
            query=r["query"],
            members=list(r["members"]),
            study_uids=list(r["study_uids"]),
            created_by=r["created_by"],
            saved=r["saved"],
            created_at=r["created_at"].isoformat(),
        )

    def create(self, cohort: Cohort) -> Cohort:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO copilot.cohorts (id, name, question, query, members, study_uids, member_count, "
                "created_by, saved) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    cohort.id,
                    cohort.name,
                    cohort.question,
                    Jsonb(cohort.query),
                    cohort.members,
                    cohort.study_uids,
                    cohort.member_count,
                    cohort.created_by,
                    cohort.saved,
                ),
            )
        return cohort

    def get(self, cohort_id: str, user: str) -> Cohort | None:
        try:
            uuid.UUID(cohort_id)
        except ValueError:
            return None
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT * FROM copilot.cohorts WHERE id = %s AND created_by = %s", (cohort_id, user)
            ).fetchone()
        return self._row(row) if row else None

    def save(self, cohort_id: str, user: str, name: str) -> Cohort | None:
        if self.get(cohort_id, user) is None:
            return None
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE copilot.cohorts SET name = %s, saved = true, updated_at = now() "
                "WHERE id = %s AND created_by = %s",
                (name[:200], cohort_id, user),
            )
        return self.get(cohort_id, user)

    def list(self, user: str, limit: int = 50) -> list[Cohort]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT * FROM copilot.cohorts WHERE created_by = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (user, limit),
            ).fetchall()
        return [self._row(r) for r in rows]


class ConversationMemory:
    """Last N turns per (user, session); bounded LRU with idle expiry. Turns hold the
    redacted question and the plan — never results or pseudonyms."""

    def __init__(self, turns: int = 6, max_sessions: int = 5000, ttl_seconds: float = 3600) -> None:
        self.turns = turns
        self.max_sessions = max_sessions
        self.ttl = ttl_seconds
        self._data: OrderedDict[str, tuple[float, deque[dict[str, Any]]]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _key(user: str, session_id: str) -> str:
        return f"{user}:{session_id}"

    def history(self, user: str, session_id: str) -> list[dict[str, Any]]:
        key = self._key(user, session_id)
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return []
            ts, turns = entry
            if time.monotonic() - ts > self.ttl:
                del self._data[key]
                return []
            return list(turns)

    def add(self, user: str, session_id: str, turn: dict[str, Any]) -> None:
        key = self._key(user, session_id)
        with self._lock:
            _, turns = self._data.pop(key, (0.0, deque(maxlen=self.turns)))
            turns.append(turn)
            self._data[key] = (time.monotonic(), turns)
            while len(self._data) > self.max_sessions:
                self._data.popitem(last=False)

    def clear(self, user: str, session_id: str) -> None:
        with self._lock:
            self._data.pop(self._key(user, session_id), None)
