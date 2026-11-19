"""End-to-end C-STORE through the gateway into a stand-in XNAT SCP (all in-process)."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydicom.dataset import Dataset
from pynetdicom import AE, AllStoragePresentationContexts, evt

from scoutoss_indexer.deid.pseudonym import pseudonymize
from scoutoss_indexer.deid.vault import MemoryVault
from scoutoss_indexer.gateway import Gateway, GatewayConfig, create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def fake_xnat() -> Any:
    received: list[Dataset] = []
    ae = AE(ae_title="XNAT")
    ae.supported_contexts = AllStoragePresentationContexts

    def on_store(event: Any) -> int:
        ds = event.dataset
        ds.file_meta = event.file_meta
        received.append(ds)
        return 0x0000

    port = _free_port()
    server = ae.start_server(
        ("127.0.0.1", port), block=False, evt_handlers=[(evt.EVT_C_STORE, on_store)]
    )
    yield port, received
    server.shutdown()


def _send(port: int, datasets: list[Dataset], called: str = "SCOUTGW") -> list[int]:
    ae = AE(ae_title="MODALITY")
    for ctx in AllStoragePresentationContexts[:50]:
        ae.add_requested_context(ctx.abstract_syntax)
    assoc = ae.associate("127.0.0.1", port, ae_title=called)
    assert assoc.is_established
    statuses = [int(assoc.send_c_store(ds).Status) for ds in datasets]
    assoc.release()
    return statuses


def test_gateway_deidentifies_and_forwards(
    tmp_path: Path, salt: bytes, fake_xnat: Any, make_ds: Callable[..., Dataset]
) -> None:
    xnat_port, received = fake_xnat
    port = _free_port()
    vault = MemoryVault({"SCOUT_DEMO": -50, "RESEARCH2": -70})
    config = GatewayConfig(
        port=port,
        spool=tmp_path / "spool",
        xnat_host="127.0.0.1",
        xnat_port=xnat_port,
        project_map={"RESEARCH2": "RESEARCH2"},
        retry_seconds=0.2,
    )
    gw = Gateway(config, salt=salt, vault=vault)
    gw.start()
    try:
        good = [make_ds(), make_ds(InstanceNumber=2)]
        bad = make_ds(BurnedInAnnotation="YES")
        assert _send(port, [*good, bad]) == [0, 0, 0xC001]
        deadline = time.time() + 10
        while len(received) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert len(received) == 2
        for ds in received:
            assert ds.PatientID == pseudonymize("MRN100001", salt)
            assert "Doe" not in str(ds.PatientName) and ds.PatientIdentityRemoved == "YES"
            assert "Project: SCOUT_DEMO" in ds.PatientComments
            assert ds.StudyDate == "20211121"
        assert vault.subjects[pseudonymize("MRN100001", salt)] == ("MRN100001", "SCOUT_DEMO")
        assert (
            gw.project_for("RESEARCH2 ") == "RESEARCH2" and gw.project_for("OTHER") == "SCOUT_DEMO"
        )
        with TestClient(create_app(gw)) as client:
            body = client.get("/health").json()
            assert body["status"] == "ok" and body["pending_forward"] == 0
            assert client.get("/ready").status_code == 200
            assert "gateway_instances_forwarded_total" in client.get("/metrics").text
    finally:
        gw.stop()


def test_gateway_retries_until_xnat_is_up(
    tmp_path: Path, salt: bytes, make_ds: Callable[..., Dataset]
) -> None:
    xnat_port = _free_port()  # nothing listening yet
    port = _free_port()
    gw = Gateway(
        GatewayConfig(
            port=port,
            spool=tmp_path / "s",
            xnat_host="127.0.0.1",
            xnat_port=xnat_port,
            retry_seconds=0.3,
        ),
        salt=salt,
        vault=MemoryVault({"SCOUT_DEMO": -1}),
    )
    gw.start()
    received: list[Dataset] = []
    server = None
    try:
        assert _send(port, [make_ds()]) == [0]
        time.sleep(0.5)
        assert gw.pending() == 1
        ae = AE(ae_title="XNAT")
        ae.supported_contexts = AllStoragePresentationContexts
        server = ae.start_server(
            ("127.0.0.1", xnat_port),
            block=False,
            evt_handlers=[(evt.EVT_C_STORE, lambda e: received.append(e.dataset) or 0)],
        )
        deadline = time.time() + 10
        while not received and time.time() < deadline:
            time.sleep(0.05)
        assert len(received) == 1
        time.sleep(0.2)
        assert gw.pending() == 0
    finally:
        gw.stop()
        if server:
            server.shutdown()


def test_gateway_handles_processing_errors(tmp_path: Path, salt: bytes) -> None:
    gw = Gateway(GatewayConfig(spool=tmp_path / "s"), salt=salt, vault=MemoryVault())

    class Assoc:
        class acceptor:  # noqa: N801
            ae_title = b"SCOUTGW"

    class Event:
        assoc = Assoc()

        @property
        def dataset(self) -> Dataset:
            raise ValueError("corrupt dataset")

    assert gw.handle_store(Event()) == 0x0110
    gw.handle_released(Event())  # nothing spooled -> no-op
    assert not gw.healthy
    empty = gw.outgoing / "empty"
    empty.mkdir()
    assert gw.forward(empty) is True and not empty.exists()


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_PROJECT_MAP", '{"AE1": "P1"}')
    monkeypatch.setenv("GATEWAY_PORT", "12345")
    cfg = GatewayConfig.from_env()
    assert cfg.port == 12345 and cfg.project_map == {"AE1": "P1"}


def test_archiver_expedites_and_retries() -> None:
    import httpx
    import respx

    from scoutoss_indexer.gateway import XnatArchiver

    base = "http://xnat.test"
    listings = iter(
        [
            {
                "ResultSet": {
                    "Result": [
                        {"name": "S1", "status": "RECEIVING", "url": "/prearchive/projects/P/t/S1"},
                        {"name": "S2", "status": "BUILDING", "url": "/prearchive/projects/P/t/S2"},
                        {
                            "name": "OTHER",
                            "status": "READY",
                            "url": "/prearchive/projects/P/t/OTHER",
                        },
                    ]
                }
            },
            {
                "ResultSet": {
                    "Result": [
                        {"name": "S2", "status": "ERROR", "url": "/prearchive/projects/P/t/S2"}
                    ]
                }
            },
            {"ResultSet": {"Result": []}},
        ]
    )
    with respx.mock() as router:
        router.get(f"{base}/data/prearchive/projects/P").mock(
            side_effect=lambda r: httpx.Response(200, json=next(listings))
        )
        build = router.post(url__regex=rf"{base}/data/prearchive/projects/P/t/S\d").respond(200)
        archive = router.post(f"{base}/data/services/archive").respond(303)
        archiver = XnatArchiver(base, "u", "p", attempts=5, delay=0)
        assert archiver.expedite("P", {"S1", "S2"}) == 2
        assert build.call_count == 2 and archive.call_count == 2
        router.get(f"{base}/data/prearchive/projects/Q").respond(500)
        assert XnatArchiver(base, "u", "p", attempts=2, delay=0).expedite("Q", {"X"}) == 0
