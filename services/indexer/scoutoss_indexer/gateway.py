"""DICOM de-identification gateway (C-STORE SCP -> de-identify -> C-STORE SCU to XNAT).

Modalities, PACS and the seed script send to this gateway, never to XNAT directly.
Each instance is de-identified in memory (see :mod:`scoutoss_indexer.deid`), the
pseudonym/offset mapping is written to the encrypted vault, and the clean instance is
spooled to disk. When the association is released the spooled instances are forwarded
to XNAT's DICOM SCP receiver. Failed forwards stay in the spool and are retried.

The called AE title selects the XNAT project (``GATEWAY_PROJECT_MAP``), defaulting to
``DEFAULT_PROJECT``.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from prometheus_client import Counter
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

from scoutoss_common.web import install_observability
from scoutoss_indexer.deid.deidentify import DeidConfig, Deidentifier, DeidRejected
from scoutoss_indexer.deid.vault import Vault

log = structlog.get_logger("scoutoss.gateway")
tracer = trace.get_tracer(__name__)
SERVICE = "deid-gateway"

RECEIVED = Counter("gateway_instances_received_total", "Instances received", ["project"])
REJECTED = Counter("gateway_instances_rejected_total", "Instances rejected", ["reason"])
FORWARDED = Counter("gateway_instances_forwarded_total", "Instances forwarded to XNAT")
FORWARD_FAILURES = Counter("gateway_forward_failures_total", "Failed forward attempts")

EXPEDITED = Counter("gateway_sessions_expedited_total", "Sessions archived on request", ["outcome"])
_COMMENT = re.compile(r"Project: (\S+) Subject: (\S+) Session: (\S+)")

STATUS_SUCCESS = 0x0000
STATUS_DEID_REJECTED = 0xC001
STATUS_PROCESSING_FAILURE = 0x0110


@dataclass
class GatewayConfig:
    ae_title: str = "SCOUTGW"
    port: int = 11112
    spool: Path = Path("/spool")
    xnat_host: str = "xnat-web"
    xnat_port: int = 8104
    xnat_ae: str = "XNAT"
    default_project: str = "SCOUT_DEMO"
    project_map: dict[str, str] = field(default_factory=dict)
    retry_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> GatewayConfig:
        return cls(
            ae_title=os.getenv("GATEWAY_AE_TITLE", "SCOUTGW"),
            port=int(os.getenv("GATEWAY_PORT", "11112")),
            spool=Path(os.getenv("GATEWAY_SPOOL", "/spool")),
            xnat_host=os.getenv("XNAT_DICOM_HOST", "xnat-web"),
            xnat_port=int(os.getenv("XNAT_DICOM_PORT", "8104")),
            xnat_ae=os.getenv("XNAT_DICOM_AE", "XNAT"),
            default_project=os.getenv("DEFAULT_PROJECT", "SCOUT_DEMO"),
            project_map=json.loads(os.getenv("GATEWAY_PROJECT_MAP", "{}") or "{}"),
            retry_seconds=float(os.getenv("GATEWAY_RETRY_SECONDS", "30")),
        )


class XnatArchiver:
    """Ask XNAT to build + archive freshly received sessions immediately.

    XNAT otherwise waits for its session rebuilder (>= 1 minute). Also retries sessions
    that failed to build (XNAT can race on its very first concurrent archive after boot).
    """

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        attempts: int = 15,
        delay: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=(user, password),
            timeout=60,
            transport=transport,
            headers={"Accept": "application/json, */*"},
        )
        self.attempts = attempts
        self.delay = delay

    def _pending(self, project: str, labels: set[str]) -> list[dict[str, Any]]:
        resp = self.http.get(f"/data/prearchive/projects/{project}", params={"format": "json"})
        resp.raise_for_status()
        rows: list[dict[str, Any]] = resp.json().get("ResultSet", {}).get("Result", [])
        return [r for r in rows if r.get("name") in labels]

    def expedite(self, project: str, labels: set[str]) -> int:
        archived: set[str] = set()
        seen = False
        for _ in range(self.attempts):
            try:
                rows = self._pending(project, labels - archived)
            except httpx.HTTPError as exc:
                log.warning("prearchive listing failed", error=type(exc).__name__)
                rows = []
            if not rows and seen:
                break
            for row in rows:
                seen = True
                url, status = str(row.get("url")), str(row.get("status", "")).upper()
                if status in {
                    "BUILDING",
                    "ARCHIVING",
                    "MOVING",
                    "QUEUED_BUILDING",
                    "QUEUED_ARCHIVING",
                }:
                    continue
                if status in {"RECEIVING", "ERROR", "_RECEIVING"}:
                    self.http.post(f"/data{url}", params={"action": "build"})
                resp = self.http.post(
                    "/data/services/archive", params={"src": url, "overwrite": "append"}
                )
                if resp.status_code in (200, 201, 301, 303):
                    archived.add(str(row.get("name")))
                    EXPEDITED.labels("archived").inc()
                else:
                    EXPEDITED.labels("retry").inc()
            if archived >= labels:
                break
            time.sleep(self.delay)
        log.info("sessions expedited", requested=len(labels), archived=len(archived))
        return len(archived)


class Gateway:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        salt: bytes,
        vault: Vault,
        archiver: XnatArchiver | None = None,
    ) -> None:
        self.config = config
        self.salt = salt
        self.vault = vault
        self.archiver = archiver
        self.incoming = config.spool / "incoming"
        self.outgoing = config.spool / "outgoing"
        for path in (self.incoming, self.outgoing):
            path.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[Path] = queue.Queue()
        self._server: Any = None
        self._stop = threading.Event()
        self._forwarder: threading.Thread | None = None

    # -------------------------------------------------------------- processing
    def project_for(self, called_ae: str) -> str:
        return self.config.project_map.get(called_ae.strip(), self.config.default_project)

    def deidentify(self, ds: Dataset, project: str) -> Dataset:
        deid = Deidentifier(
            DeidConfig(
                salt=self.salt, project=project, offset_days=self.vault.project_offset(project)
            )
        )
        result = deid.apply(ds)
        self.vault.record_subject(result.pseudonym, result.original_patient_id, project)
        return ds

    def handle_store(self, event: Any) -> int:
        """pynetdicom ``EVT_C_STORE`` handler."""
        called = event.assoc.acceptor.ae_title
        called = called.decode() if isinstance(called, bytes) else str(called)
        project = self.project_for(called)
        assoc_dir = self.incoming / f"assoc-{id(event.assoc)}"
        try:
            ds = event.dataset
            ds.file_meta = event.file_meta
            self.deidentify(ds, project)
            assoc_dir.mkdir(parents=True, exist_ok=True)
            ds.save_as(assoc_dir / f"{uuid.uuid4().hex}.dcm", enforce_file_format=True)
        except DeidRejected as exc:
            REJECTED.labels(str(exc)[:40]).inc()
            log.warning("instance rejected by de-identification", reason=str(exc))
            return STATUS_DEID_REJECTED
        except Exception as exc:
            REJECTED.labels("error").inc()
            log.error("instance processing failed", error=type(exc).__name__)
            return STATUS_PROCESSING_FAILURE
        RECEIVED.labels(project).inc()
        return STATUS_SUCCESS

    def handle_released(self, event: Any) -> None:
        """Move this association's spooled files to the outgoing queue."""
        assoc_dir = self.incoming / f"assoc-{id(event.assoc)}"
        if not assoc_dir.exists():
            return
        batch = self.outgoing / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        shutil.move(str(assoc_dir), str(batch))
        count = len(list(batch.glob("*.dcm")))
        log.info("association complete", instances=count)
        self._queue.put(batch)

    # -------------------------------------------------------------- forwarding
    def forward(self, batch: Path) -> bool:
        from pynetdicom import AE

        files = sorted(batch.glob("*.dcm"))
        if not files:
            shutil.rmtree(batch, ignore_errors=True)
            return True
        import pydicom

        contexts: set[tuple[str, str]] = set()
        sessions: dict[str, set[str]] = {}
        for f in files:
            header = pydicom.dcmread(f, stop_before_pixels=True)
            meta = header.file_meta
            contexts.add((str(meta.MediaStorageSOPClassUID), str(meta.TransferSyntaxUID)))
            match = _COMMENT.search(str(header.get("PatientComments", "")))
            if match:
                sessions.setdefault(match.group(1), set()).add(match.group(3))
        ae = AE(ae_title=self.config.ae_title)
        for sop_class, syntax in sorted(contexts)[:120]:
            ae.add_requested_context(
                sop_class,
                list(dict.fromkeys([syntax, ExplicitVRLittleEndian, ImplicitVRLittleEndian])),
            )
        with tracer.start_as_current_span("gateway.forward", attributes={"instances": len(files)}):
            assoc = ae.associate(
                self.config.xnat_host, self.config.xnat_port, ae_title=self.config.xnat_ae
            )
            if not assoc.is_established:
                FORWARD_FAILURES.inc()
                log.warning("XNAT association failed; will retry", instances=len(files))
                return False
            sent = 0
            try:
                for f in files:
                    status = assoc.send_c_store(f)
                    if status and getattr(status, "Status", 0xFFFF) in (
                        0x0000,
                        0xB000,
                        0xB007,
                        0xB006,
                    ):
                        f.unlink()
                        sent += 1
            finally:
                assoc.release()
        FORWARDED.inc(sent)
        log.info("forwarded to XNAT", instances=sent, remaining=len(files) - sent)
        if sent == len(files):
            shutil.rmtree(batch, ignore_errors=True)
            if self.archiver is not None and sessions:
                threading.Thread(target=self._expedite, args=(sessions,), daemon=True).start()
            return True
        FORWARD_FAILURES.inc()
        return False

    def _expedite(self, sessions: dict[str, set[str]]) -> None:
        assert self.archiver is not None
        for project, labels in sessions.items():
            try:
                self.archiver.expedite(project, labels)
            except Exception as exc:  # XNAT's own rebuilder remains the fallback
                log.warning("expedite failed", error=type(exc).__name__)

    def _forward_loop(self) -> None:
        for existing in sorted(self.outgoing.iterdir()):  # resume after restart
            self._queue.put(existing)
        while not self._stop.is_set():
            try:
                batch = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                ok = self.forward(batch)
            except Exception as exc:
                log.error("forward error", error=type(exc).__name__)
                ok = False
            if not ok and not self._stop.is_set():
                threading.Timer(self.config.retry_seconds, self._queue.put, args=(batch,)).start()

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        from pynetdicom import AE, ALL_TRANSFER_SYNTAXES, AllStoragePresentationContexts, evt
        from pynetdicom.sop_class import Verification  # type: ignore[attr-defined]

        ae = AE(ae_title=self.config.ae_title)
        for ctx in AllStoragePresentationContexts:
            if ctx.abstract_syntax:
                ae.add_supported_context(ctx.abstract_syntax, ALL_TRANSFER_SYNTAXES)
        ae.add_supported_context(Verification)
        ae.require_called_aet = False
        handlers: list[Any] = [
            (evt.EVT_C_STORE, self.handle_store),
            (evt.EVT_RELEASED, self.handle_released),
            (evt.EVT_ABORTED, self.handle_released),
        ]
        self._server = ae.start_server(
            ("0.0.0.0", self.config.port),  # noqa: S104 - container listener
            block=False,
            evt_handlers=handlers,
        )
        self._forwarder = threading.Thread(target=self._forward_loop, name="forwarder", daemon=True)
        self._forwarder.start()
        log.info("gateway listening", port=self.config.port, ae_title=self.config.ae_title)

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()

    @property
    def healthy(self) -> bool:
        return self._server is not None and bool(self._forwarder and self._forwarder.is_alive())

    def pending(self) -> int:
        return sum(1 for _ in self.outgoing.rglob("*.dcm"))


def create_app(gateway: Gateway) -> FastAPI:
    app = FastAPI(title="ScoutOSS DICOM de-identification gateway")
    install_observability(app, SERVICE)

    @app.get("/health")
    def health() -> dict[str, Any]:
        if not gateway.healthy:
            raise HTTPException(503, "gateway not running")
        return {"status": "ok", "service": SERVICE, "pending_forward": gateway.pending()}

    @app.get("/ready")
    def ready() -> dict[str, Any]:
        return health()

    return app


def main() -> int:  # pragma: no cover - process entry point
    import uvicorn

    from scoutoss_common.logging import configure_logging
    from scoutoss_common.tracing import setup_tracing
    from scoutoss_indexer.bootstrap import build_vault
    from scoutoss_indexer.deid.vault import PostgresVault
    from scoutoss_indexer.settings import get_settings

    configure_logging(SERVICE)
    settings = get_settings()
    vault = build_vault(settings)
    if isinstance(vault, PostgresVault):
        vault.migrate()
    archiver = None
    if os.getenv("XNAT_ARCHIVER_PASSWORD"):
        archiver = XnatArchiver(
            os.getenv("XNAT_URL", "http://xnat-web:8080"),
            os.getenv("XNAT_ARCHIVER_USER", "scout_archiver"),
            os.environ["XNAT_ARCHIVER_PASSWORD"],
        )
    gateway = Gateway(
        GatewayConfig.from_env(), salt=settings.salt_bytes(), vault=vault, archiver=archiver
    )
    gateway.start()
    app = create_app(gateway)
    setup_tracing(SERVICE, app)
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - container listener
        port=int(os.getenv("PORT", "8001")),
        log_config=None,
        access_log=False,
    )
    gateway.stop()
    return 0
