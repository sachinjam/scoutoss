"""DICOM export: send AI results from XNAT to a DICOM destination (a research PACS, a viewer...).

Pipelines run by the XNAT Container Service that want their result returned over DICOM write
DICOM objects into the session resource ``AI_DICOM`` (the QC pipeline writes a Secondary Capture
summary page). This job finds new files in that resource, checks each one and C-STOREs it to the
configured destination:

* only objects that belong to the session's own study and carry a ScoutOSS pseudonym as
  ``PatientID`` are sent, so a container cannot use the export to move anything else out;
* every send is written to the audit trail first; if the trail is unavailable nothing is sent;
* what was sent is recorded per file (name + checksum), so a file is sent once and a changed file
  is sent again. A destination that is down stops the cycle without using up retries.

Nothing is re-identified: the destination receives the same pseudonymous patient and study
identifiers that XNAT holds. Sending results into a clinical PACS under the real patient would
need the re-identification vault and is deliberately not done here.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
import pydicom
import structlog
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

from scoutoss_common.audit import AuditError, AuditLogger
from scoutoss_common.xnat_session import XnatSessionAuth
from scoutoss_indexer.deid.pseudonym import is_pseudonym

log = structlog.get_logger(__name__)

RESOURCE = "AI_DICOM"
AUDIT_USER = "service:dicom-export"
SENT, REJECTED, ERROR = "sent", "rejected", "error"

DDL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'indexer') THEN
        CREATE SCHEMA indexer;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS indexer.dicom_export (
    experiment_id text NOT NULL,
    file_key      text NOT NULL,
    destination   text NOT NULL,
    status        text NOT NULL CHECK (status IN ('sent', 'rejected', 'error')),
    attempts      integer NOT NULL DEFAULT 1,
    study_uid     text,
    series_uid    text,
    last_error    text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (experiment_id, file_key, destination)
);
"""


class DestinationUnavailable(RuntimeError):
    """The destination did not accept an association (down, wrong AE title, refused)."""


@dataclass(frozen=True)
class Destination:
    host: str
    port: int
    ae_title: str
    calling_ae: str = "SCOUTOSS"

    @property
    def name(self) -> str:
        return f"{self.ae_title}@{self.host}:{self.port}"


@dataclass(frozen=True)
class Entry:
    status: str
    attempts: int
    study_uid: str | None = None
    series_uid: str | None = None


Key = tuple[str, str]  # (experiment id, file key)


class ExportState(Protocol):
    def entries(self, destination: str) -> dict[Key, Entry]: ...

    def mark(
        self,
        key: Key,
        destination: str,
        status: str,
        *,
        study_uid: str | None = None,
        series_uid: str | None = None,
        error: str | None = None,
    ) -> None: ...


class MemoryState:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], Entry] = {}

    def entries(self, destination: str) -> dict[Key, Entry]:
        return {(e, f): row for (e, f, d), row in self.rows.items() if d == destination}

    def mark(
        self,
        key: Key,
        destination: str,
        status: str,
        *,
        study_uid: str | None = None,
        series_uid: str | None = None,
        error: str | None = None,
    ) -> None:
        prev = self.rows.get((*key, destination))
        attempts = (prev.attempts if prev else 0) + 1
        self.rows[(*key, destination)] = Entry(status, attempts, study_uid, series_uid)


class PostgresState:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    def migrate(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(DDL)

    def entries(self, destination: str) -> dict[Key, Entry]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT experiment_id, file_key, status, attempts, study_uid, series_uid "
                "FROM indexer.dicom_export WHERE destination = %s",
                (destination,),
            ).fetchall()
        return {(r[0], r[1]): Entry(r[2], int(r[3]), r[4], r[5]) for r in rows}

    def mark(
        self,
        key: Key,
        destination: str,
        status: str,
        *,
        study_uid: str | None = None,
        series_uid: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO indexer.dicom_export
                    (experiment_id, file_key, destination, status, study_uid, series_uid,
                     last_error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (experiment_id, file_key, destination) DO UPDATE SET
                    status = EXCLUDED.status,
                    attempts = indexer.dicom_export.attempts + 1,
                    study_uid = EXCLUDED.study_uid,
                    series_uid = EXCLUDED.series_uid,
                    last_error = EXCLUDED.last_error,
                    updated_at = now()
                """,
                (*key, destination, status, study_uid, series_uid, error),
            )


class XnatResults:
    """Reads sessions and their result resource from XNAT (a read-only service account)."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        resource: str = RESOURCE,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.resource = resource
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=XnatSessionAuth(base_url, user, password),
            timeout=60,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def _rows(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        resp = self.http.get(path, params={"format": "json", **(params or {})})
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        rows: list[dict[str, Any]] = resp.json().get("ResultSet", {}).get("Result", [])
        return rows

    def sessions(self) -> list[dict[str, Any]]:
        """Image sessions with their (de-identified) StudyInstanceUID."""
        rows = self._rows("/data/experiments", {"columns": "ID,project,xsiType,UID"})
        return [r for r in rows if str(r.get("xsiType", "")).endswith("SessionData")]

    def files(self, experiment_id: str) -> list[dict[str, Any]]:
        return self._rows(f"/data/experiments/{experiment_id}/resources/{self.resource}/files")

    def download(self, uri: str) -> bytes:
        resp = self.http.get(uri, headers={"Accept": "application/dicom, */*"})
        resp.raise_for_status()
        return resp.content


class Sender(Protocol):
    def send(self, datasets: list[Dataset]) -> list[bool]: ...


class DicomSender:
    """C-STORE (and C-FIND, to verify) against one destination with pynetdicom."""

    def __init__(self, destination: Destination) -> None:
        self.destination = destination

    def _associate(self, ae: Any) -> Any:
        ae.acse_timeout, ae.dimse_timeout, ae.network_timeout = 15, 120, 60
        dest = self.destination
        assoc = ae.associate(dest.host, dest.port, ae_title=dest.ae_title)
        if not assoc.is_established:
            raise DestinationUnavailable(dest.name)
        return assoc

    def send(self, datasets: list[Dataset]) -> list[bool]:
        """One association for the batch; ``True`` per object the destination stored."""
        from pynetdicom import AE

        ae = AE(ae_title=self.destination.calling_ae)
        syntaxes: dict[str, set[str]] = {}
        for ds in datasets:
            own = str(getattr(ds.file_meta, "TransferSyntaxUID", ExplicitVRLittleEndian))
            syntaxes.setdefault(str(ds.SOPClassUID), set()).add(own)
        for sop_class, own_syntaxes in syntaxes.items():
            wanted = [*sorted(own_syntaxes), ExplicitVRLittleEndian, ImplicitVRLittleEndian]
            ae.add_requested_context(sop_class, list(dict.fromkeys(wanted)))
        assoc = self._associate(ae)
        stored: list[bool] = []
        try:
            for ds in datasets:
                try:
                    status = assoc.send_c_store(ds)
                except (ValueError, RuntimeError):  # no accepted context for this object
                    stored.append(False)
                    continue
                # 0x0000 success; 0xB000-0xB007 stored with a warning (e.g. coerced elements)
                code = int(status.Status) if status and "Status" in status else -1
                stored.append(code == 0 or 0xB000 <= code <= 0xB007)
        finally:
            assoc.release()
        return stored

    def series_at_destination(self, study_uid: str) -> set[str]:
        """SeriesInstanceUIDs the destination holds for a study (C-FIND, study root)."""
        from pynetdicom import AE
        from pynetdicom.sop_class import (  # type: ignore[attr-defined]
            StudyRootQueryRetrieveInformationModelFind,
        )

        ae = AE(ae_title=self.destination.calling_ae)
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
        assoc = self._associate(ae)
        query = Dataset()
        query.QueryRetrieveLevel = "SERIES"
        query.StudyInstanceUID = study_uid
        query.SeriesInstanceUID = ""
        found: set[str] = set()
        try:
            for status, identifier in assoc.send_c_find(
                query, StudyRootQueryRetrieveInformationModelFind
            ):
                if status and status.Status in (0xFF00, 0xFF01) and identifier is not None:
                    found.add(str(identifier.SeriesInstanceUID))
        finally:
            assoc.release()
        return found


def file_key(row: dict[str, Any]) -> str:
    """Name plus checksum, or size where XNAT has no checksum (it has none for files stored by
    the Container Service). A pipeline that regenerates a result must write it under a new name
    to have it exported again."""
    return f"{row.get('Name')}:{row.get('digest') or row.get('Size') or ''}"


def check(blob: bytes, study_uid: str) -> tuple[Dataset | None, str | None]:
    """Parse a result file and decide whether it may leave. Returns ``(dataset, None)`` or
    ``(None, reason)``; reasons never contain header values."""
    try:
        ds = pydicom.dcmread(io.BytesIO(blob))
    except (InvalidDicomError, OSError, ValueError, KeyError):
        return None, "not a DICOM file"
    if not ds.get("SOPClassUID") or not ds.get("SOPInstanceUID"):
        return None, "no SOP class or instance UID"
    if not study_uid or str(ds.get("StudyInstanceUID", "")) != study_uid:
        return None, "not in the session's study"
    if not is_pseudonym(str(ds.get("PatientID", ""))):
        return None, "PatientID is not a ScoutOSS pseudonym"
    return ds, None


@dataclass
class ExportResult:
    sent: int = 0
    rejected: int = 0
    failed: int = 0
    sessions: list[str] = field(default_factory=list)


def export_once(
    xnat: XnatResults,
    state: ExportState,
    sender: Sender,
    audit: AuditLogger,
    destination: Destination,
    *,
    max_attempts: int = 5,
) -> ExportResult:
    """Send every result file that has not been sent to ``destination`` yet."""
    result = ExportResult()
    known = state.entries(destination.name)
    for session in xnat.sessions():
        exp_id, study_uid = str(session.get("ID")), str(session.get("UID") or "")
        todo = []
        for row in xnat.files(exp_id):
            key = (exp_id, file_key(row))
            entry = known.get(key)
            if entry is None or (entry.status == ERROR and entry.attempts < max_attempts):
                todo.append((key, row))
        if not todo:
            continue
        batch: list[tuple[Key, Dataset]] = []
        for key, row in todo:
            ds, reason = check(xnat.download(str(row["URI"])), study_uid)
            if ds is None:
                state.mark(key, destination.name, REJECTED, error=reason)
                result.rejected += 1
                log.warning("result file not exported", experiment=exp_id, reason=reason)
            else:
                batch.append((key, ds))
        if not batch:
            continue
        params = {"destination": destination.name, "objects": len(batch)}
        try:
            audit.record(
                user=AUDIT_USER,
                action="dicom_export",
                resource=exp_id,
                params=params,
                outcome="attempted",
            )
        except AuditError:
            log.error("dicom export skipped: audit trail unavailable", experiment=exp_id)
            continue
        stored = sender.send([ds for _, ds in batch])
        for (key, ds), ok in zip(batch, stored, strict=True):
            state.mark(
                key,
                destination.name,
                SENT if ok else ERROR,
                study_uid=study_uid,
                series_uid=str(ds.get("SeriesInstanceUID", "")),
                error=None if ok else "destination did not store the object",
            )
        result.sent += sum(stored)
        result.failed += len(stored) - sum(stored)
        result.sessions.append(exp_id)
        if not all(stored):
            audit.record(
                user=AUDIT_USER,
                action="dicom_export",
                resource=exp_id,
                params=params,
                outcome="failure",
            )
    if result.sent or result.rejected or result.failed:
        log.info(
            "dicom export cycle",
            destination=destination.name,
            sent=result.sent,
            rejected=result.rejected,
            failed=result.failed,
            sessions=len(result.sessions),
        )
    return result


def status(state: ExportState, destination: Destination) -> dict[str, Any]:
    entries = state.entries(destination.name)
    counts = dict.fromkeys((SENT, REJECTED, ERROR), 0)
    for entry in entries.values():
        counts[entry.status] += 1
    sessions = sorted({exp for (exp, _), e in entries.items() if e.status == SENT})
    return {"destination": destination.name, **counts, "sessions_sent": sessions}


def verify(state: ExportState, sender: DicomSender, destination: Destination) -> dict[str, Any]:
    """Ask the destination (C-FIND) whether it holds every series recorded as sent."""
    wanted: dict[str, set[str]] = {}
    owner: dict[str, str] = {}
    for (exp, _), entry in state.entries(destination.name).items():
        if entry.status == SENT and entry.study_uid and entry.series_uid:
            wanted.setdefault(entry.study_uid, set()).add(entry.series_uid)
            owner[entry.study_uid] = exp
    missing = sorted(
        owner[study]
        for study, series in wanted.items()
        if not series <= sender.series_at_destination(study)
    )
    return {
        "destination": destination.name,
        "studies_sent": len(wanted),
        "studies_at_destination": len(wanted) - len(missing),
        "missing": missing,
    }


def run_forever(
    xnat: XnatResults,
    state: ExportState,
    sender: Sender,
    audit: AuditLogger,
    destination: Destination,
    *,
    interval: float,
    max_attempts: int = 5,
    heartbeat: Path | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Export every ``interval`` seconds. A failed cycle is logged and retried; it never exits.
    The heartbeat is touched after every cycle that reached XNAT, also while the destination is
    down: restarting this process does not bring a PACS back."""
    stop = stop or threading.Event()
    log.info("dicom export started", destination=destination.name, interval_seconds=interval)
    while not stop.is_set():
        try:
            export_once(xnat, state, sender, audit, destination, max_attempts=max_attempts)
            if heartbeat is not None:
                heartbeat.touch()
        except DestinationUnavailable:
            log.warning("dicom destination unavailable", destination=destination.name)
            if heartbeat is not None:
                heartbeat.touch()
        except (httpx.HTTPError, KeyError, ValueError, OSError) as exc:
            log.warning("dicom export failed", error=type(exc).__name__, detail=str(exc)[:200])
        stop.wait(interval)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entry point
    from psycopg_pool import ConnectionPool

    from scoutoss_common.logging import configure_logging
    from scoutoss_indexer.settings import get_settings
    from scoutoss_indexer.store import make_client

    parser = argparse.ArgumentParser(prog="scoutoss-indexer dicom-export")
    parser.add_argument("--status", action="store_true", help="print what has been sent and exit")
    parser.add_argument(
        "--verify", action="store_true", help="check the destination holds what was sent and exit"
    )
    args = parser.parse_args(argv)
    configure_logging("dicom-export")
    settings = get_settings()
    destination = Destination(
        os.environ["DICOM_EXPORT_HOST"],
        int(os.environ.get("DICOM_EXPORT_PORT", "104")),
        os.environ["DICOM_EXPORT_AE"],
        os.environ.get("DICOM_EXPORT_CALLING_AE", "SCOUTOSS"),
    )
    state = PostgresState(ConnectionPool(settings.database_url, min_size=1, max_size=2, open=True))
    state.migrate()
    sender = DicomSender(destination)
    if args.status:
        print(json.dumps(status(state, destination)))
        return 0
    if args.verify:
        report = verify(state, sender, destination)
        print(json.dumps(report))
        return 1 if report["missing"] else 0
    xnat = XnatResults(
        settings.xnat_url,
        settings.xnat_user,
        settings.xnat_secret(),
        resource=os.environ.get("DICOM_EXPORT_RESOURCE", RESOURCE),
    )
    audit = AuditLogger(
        make_client(
            settings.opensearch_url,
            settings.opensearch_user,
            settings.opensearch_password,
            settings.opensearch_verify_certs,
        ),
        service="dicom-export",
    )
    run_forever(
        xnat,
        state,
        sender,
        audit,
        destination,
        interval=float(os.environ.get("DICOM_EXPORT_SECONDS", "15")),
        max_attempts=int(os.environ.get("DICOM_EXPORT_MAX_ATTEMPTS", "5")),
        heartbeat=Path(os.environ.get("DICOM_EXPORT_HEARTBEAT", "/tmp/dicom-export.ok")),  # noqa: S108
    )
    return 0
