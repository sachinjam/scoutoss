"""XNAT REST source.

Polls ``/data/experiments`` for image sessions, compares each against the Postgres
cursor (``change_token`` = XNAT's last-modified/insert timestamp) and, for new or changed
sessions, downloads ONE DICOM file per scan to read the series header. The instance
count comes from the scan's file listing, so pixel data is never pulled in bulk.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from scoutoss_indexer.cursor import CursorStore, needs_processing
from scoutoss_indexer.pipeline import WorkItem

log = structlog.get_logger(__name__)

SESSION_COLUMNS = "ID,label,project,xsiType,insert_date,last_modified,subject_ID,subject_label"


class XnatClient:
    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=(user, password),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self.http.close()

    def _results(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        resp = self.http.get(path, params={"format": "json", **(params or {})})
        resp.raise_for_status()
        payload = resp.json()
        rows: list[dict[str, Any]] = payload.get("ResultSet", {}).get("Result", [])
        return rows

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self._results("/data/experiments", {"columns": SESSION_COLUMNS})
        return [r for r in rows if str(r.get("xsiType", "")).endswith("SessionData")]

    def list_scans(self, experiment_id: str) -> list[dict[str, Any]]:
        return self._results(f"/data/experiments/{experiment_id}/scans")

    def list_files(self, experiment_id: str, scan_id: str) -> list[dict[str, Any]]:
        try:
            return self._results(
                f"/data/experiments/{experiment_id}/scans/{scan_id}/resources/DICOM/files"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise

    def download(self, uri: str) -> bytes:
        resp = self.http.get(uri, headers={"Accept": "application/dicom, */*"})
        resp.raise_for_status()
        return resp.content

    def ping(self) -> bool:
        try:
            return self.http.get("/data/version", timeout=5).status_code < 500
        except httpx.HTTPError:
            return False


@dataclass
class SessionWork:
    experiment_id: str
    project: str | None
    change_token: str
    items: list[WorkItem] = field(default_factory=list)


def change_token(row: dict[str, Any]) -> str:
    return str(row.get("last_modified") or row.get("insert_date") or "0")


class XnatSource:
    name = "xnat"

    def __init__(self, client: XnatClient, cursor: CursorStore, *, max_attempts: int = 5) -> None:
        self.client = client
        self.cursor = cursor
        self.max_attempts = max_attempts

    def pending_sessions(self, only: set[str] | None = None) -> list[dict[str, Any]]:
        sessions = self.client.list_sessions()
        known = self.cursor.get_all()
        todo = []
        for row in sessions:
            exp_id = str(row.get("ID"))
            if only is not None and exp_id not in only:
                continue
            if needs_processing(known.get(exp_id), change_token(row), self.max_attempts):
                todo.append(row)
        return todo

    def build(self, row: dict[str, Any]) -> SessionWork:
        exp_id = str(row["ID"])
        work = SessionWork(
            experiment_id=exp_id, project=row.get("project"), change_token=change_token(row)
        )
        for scan in self.client.list_scans(exp_id):
            scan_id = str(scan.get("ID"))
            files = [
                f
                for f in self.client.list_files(exp_id, scan_id)
                if f.get("URI") and not str(f.get("Name", "")).lower().endswith(".xml")
            ]
            if not files:
                continue
            uri = str(files[0]["URI"])
            work.items.append(
                WorkItem(
                    ref=f"xnat:{exp_id}/scans/{scan_id}",
                    load=functools.partial(self.client.download, uri),
                    source=self.name,
                    instance_count=len(files),
                    context={
                        "project": row.get("project"),
                        "experiment_id": exp_id,
                        "session_label": row.get("label"),
                        "subject_id": row.get("subject_ID"),
                        "subject_label": row.get("subject_label"),
                        "scan_id": scan_id,
                    },
                )
            )
        return work
