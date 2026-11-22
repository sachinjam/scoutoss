"""DICOM export of AI results: what is sent, what is refused, retries, audit, and a real C-STORE."""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

from scoutoss_common.audit import AuditLogger
from scoutoss_indexer.deid.pseudonym import pseudonymize
from scoutoss_indexer.dicom_export import (
    ERROR,
    REJECTED,
    SENT,
    Destination,
    DestinationUnavailable,
    DicomSender,
    MemoryState,
    XnatResults,
    check,
    export_once,
    run_forever,
    status,
    verify,
)
from tests.support.fake_opensearch import FakeOpenSearch

XNAT = "http://xnat.test"
DEST = Destination("pacs.test", 104, "RESEARCHPACS")
STUDY = "2.25.111"


def result_object(salt: bytes, *, study: str = STUDY, patient: str | None = None) -> Dataset:
    ds = Dataset()
    ds.PatientID = patient if patient is not None else pseudonymize("MRN-1", salt)
    ds.StudyInstanceUID = study
    ds.SeriesInstanceUID = generate_uid(entropy_srcs=[study, "series"])
    ds.SOPInstanceUID = generate_uid(entropy_srcs=[study, "page"])
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.Modality = "OT"
    ds.Rows, ds.Columns, ds.SamplesPerPixel = 2, 2, 1
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = b"\x00\x01\x02\x03"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return ds


def as_bytes(ds: Dataset) -> bytes:
    buffer = io.BytesIO()
    ds.save_as(buffer, enforce_file_format=True)
    return buffer.getvalue()


def result_set(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ResultSet": {"Result": rows}}


def mock_xnat(mock: respx.MockRouter, files: dict[str, dict[str, bytes]]) -> None:
    """``files``: experiment id -> {file name: content}. E2 never has a result resource."""
    sessions = [{"ID": exp, "xsiType": "xnat:ctSessionData", "UID": STUDY} for exp in files]
    sessions.append({"ID": "QC1", "xsiType": "xnat:qcAssessmentData", "UID": ""})
    mock.get(f"{XNAT}/data/experiments").respond(json=result_set(sessions))
    for exp, content in files.items():
        listing = mock.get(f"{XNAT}/data/experiments/{exp}/resources/AI_DICOM/files")
        if not content:
            listing.respond(404)
            continue
        rows = [
            {"Name": name, "Size": str(len(blob)), "digest": "", "URI": f"/files/{exp}/{name}"}
            for name, blob in content.items()
        ]
        listing.respond(json=result_set(rows))
        for name, blob in content.items():
            mock.get(f"{XNAT}/files/{exp}/{name}").respond(content=blob)


class FakeSender:
    def __init__(self, accept: bool = True, up: bool = True) -> None:
        self.accept, self.up = accept, up
        self.batches: list[list[Dataset]] = []

    def send(self, datasets: list[Dataset]) -> list[bool]:
        if not self.up:
            raise DestinationUnavailable(DEST.name)
        self.batches.append(datasets)
        return [self.accept] * len(datasets)


@pytest.fixture
def xnat() -> XnatResults:
    return XnatResults(XNAT, "scout_indexer", "pw")


@pytest.fixture
def audit(fake_os: FakeOpenSearch) -> AuditLogger:
    return AuditLogger(fake_os, service="dicom-export")


def test_sends_new_results_once_and_audits_them(
    xnat: XnatResults,
    audit: AuditLogger,
    fake_os: FakeOpenSearch,
    salt: bytes,
    respx_mock: respx.MockRouter,
) -> None:
    mock_xnat(respx_mock, {"E1": {"qc.dcm": as_bytes(result_object(salt))}, "E2": {}})
    state, sender = MemoryState(), FakeSender()
    first = export_once(xnat, state, sender, audit, DEST)
    assert (first.sent, first.rejected, first.failed, first.sessions) == (1, 0, 0, ["E1"])
    assert sender.batches[0][0].StudyInstanceUID == STUDY
    records = fake_os.docs("audit")
    assert [(r["action"], r["resource"], r["outcome"]) for r in records] == [
        ("dicom_export", "E1", "attempted")
    ]
    again = export_once(xnat, state, sender, audit, DEST)
    assert again.sent == 0 and len(sender.batches) == 1  # nothing new: not downloaded or re-sent
    assert status(state, DEST) == {
        "destination": "RESEARCHPACS@pacs.test:104",
        SENT: 1,
        REJECTED: 0,
        ERROR: 0,
        "sessions_sent": ["E1"],
    }


def test_changed_file_is_sent_again(
    xnat: XnatResults, audit: AuditLogger, salt: bytes, respx_mock: respx.MockRouter
) -> None:
    state, sender = MemoryState(), FakeSender()
    mock_xnat(respx_mock, {"E1": {"qc.dcm": as_bytes(result_object(salt))}})
    export_once(xnat, state, sender, audit, DEST)
    bigger = result_object(salt)
    bigger.SeriesDescription = "second run"
    respx_mock.reset()
    respx_mock.clear()
    mock_xnat(respx_mock, {"E1": {"qc.dcm": as_bytes(bigger)}})
    assert export_once(xnat, state, sender, audit, DEST).sent == 1
    assert sender.batches[-1][0].SeriesDescription == "second run"


def test_refuses_objects_that_should_not_leave(
    xnat: XnatResults,
    audit: AuditLogger,
    fake_os: FakeOpenSearch,
    salt: bytes,
    respx_mock: respx.MockRouter,
) -> None:
    mock_xnat(
        respx_mock,
        {
            "E1": {
                "other-study.dcm": as_bytes(result_object(salt, study="2.25.999")),
                "real-id.dcm": as_bytes(result_object(salt, patient="MRN-12345")),
                "notes.txt": b"not dicom at all",
            }
        },
    )
    state, sender = MemoryState(), FakeSender()
    result = export_once(xnat, state, sender, audit, DEST)
    assert (result.sent, result.rejected) == (0, 3)
    assert sender.batches == [] and fake_os.docs("audit") == []
    # refused files are remembered, not retried every cycle
    assert export_once(xnat, state, sender, audit, DEST).rejected == 0


def test_check_reasons_never_quote_header_values(salt: bytes) -> None:
    no_uid = result_object(salt)
    del no_uid.SOPInstanceUID
    cases = [
        (b"junk", "not a DICOM file"),
        (as_bytes(result_object(salt, study="2.25.999")), "not in the session's study"),
        (as_bytes(result_object(salt, patient="MRN-12345")), "PatientID is not"),
    ]
    for blob, reason in cases:
        ds, why = check(blob, STUDY)
        assert ds is None and why is not None and reason in why and "MRN" not in why
    assert check(as_bytes(result_object(salt)), "")[1] == "not in the session's study"
    ok, why = check(as_bytes(result_object(salt)), STUDY)
    assert ok is not None and why is None


def test_object_the_destination_refuses_is_retried_up_to_the_limit(
    xnat: XnatResults,
    audit: AuditLogger,
    fake_os: FakeOpenSearch,
    salt: bytes,
    respx_mock: respx.MockRouter,
) -> None:
    mock_xnat(respx_mock, {"E1": {"qc.dcm": as_bytes(result_object(salt))}})
    state, sender = MemoryState(), FakeSender(accept=False)
    for _ in range(4):
        export_once(xnat, state, sender, audit, DEST, max_attempts=2)
    assert len(sender.batches) == 2  # two attempts, then left alone
    assert status(state, DEST)[ERROR] == 1
    outcomes = [r["outcome"] for r in fake_os.docs("audit")]
    assert outcomes == ["attempted", "failure", "attempted", "failure"]
    sender.accept = True
    assert export_once(xnat, state, sender, audit, DEST, max_attempts=3).sent == 1


def test_nothing_is_sent_without_an_audit_trail(
    xnat: XnatResults,
    audit: AuditLogger,
    fake_os: FakeOpenSearch,
    salt: bytes,
    respx_mock: respx.MockRouter,
) -> None:
    mock_xnat(respx_mock, {"E1": {"qc.dcm": as_bytes(result_object(salt))}})
    fake_os.fail_index.add("audit")
    state, sender = MemoryState(), FakeSender()
    assert export_once(xnat, state, sender, audit, DEST).sent == 0
    assert sender.batches == [] and state.rows == {}
    fake_os.fail_index.clear()
    assert export_once(xnat, state, sender, audit, DEST).sent == 1  # sent once the trail is back


def test_destination_outage_uses_no_retries_and_loop_keeps_running(
    xnat: XnatResults,
    audit: AuditLogger,
    salt: bytes,
    respx_mock: respx.MockRouter,
    tmp_path: Path,
) -> None:
    mock_xnat(respx_mock, {"E1": {"qc.dcm": as_bytes(result_object(salt))}})
    state, sender = MemoryState(), FakeSender(up=False)
    with pytest.raises(DestinationUnavailable):
        export_once(xnat, state, sender, audit, DEST)
    assert state.rows == {}

    stop, heartbeat, cycles = threading.Event(), tmp_path / "ok", []

    class Flaky(FakeSender):
        def send(self, datasets: list[Dataset]) -> list[bool]:
            cycles.append(len(cycles))
            if len(cycles) == 1:
                raise DestinationUnavailable(DEST.name)
            stop.set()
            return super().send(datasets)

    run_forever(xnat, state, Flaky(), audit, DEST, interval=0.01, heartbeat=heartbeat, stop=stop)
    assert len(cycles) == 2 and heartbeat.exists()
    assert status(state, DEST)[SENT] == 1


def test_loop_survives_an_xnat_outage(audit: AuditLogger, tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) >= 2:
            stop.set()
        return httpx.Response(503)

    stop, heartbeat = threading.Event(), tmp_path / "ok"
    xnat = XnatResults(XNAT, "u", "p", transport=httpx.MockTransport(handler))
    run_forever(
        xnat,
        MemoryState(),
        FakeSender(),
        audit,
        DEST,
        interval=0.01,
        heartbeat=heartbeat,
        stop=stop,
    )
    assert len(calls) >= 2 and not heartbeat.exists()  # no heartbeat while XNAT is unreachable


def test_real_c_store_and_c_find_against_a_dicom_server(salt: bytes) -> None:
    """A pynetdicom storage SCP on loopback: the object arrives and verify() finds its series."""
    from pynetdicom import AE, evt
    from pynetdicom.sop_class import (  # type: ignore[attr-defined]
        StudyRootQueryRetrieveInformationModelFind,
    )

    received: list[Dataset] = []

    def on_store(event: Any) -> int:
        received.append(event.dataset)
        return 0x0000

    def on_find(event: Any) -> Any:
        for ds in received:
            if ds.StudyInstanceUID == event.identifier.StudyInstanceUID:
                match = Dataset()
                match.QueryRetrieveLevel = "SERIES"
                match.StudyInstanceUID = ds.StudyInstanceUID
                match.SeriesInstanceUID = ds.SeriesInstanceUID
                yield 0xFF00, match

    scp = AE(ae_title="RESEARCHPACS")
    scp.add_supported_context(SecondaryCaptureImageStorage)
    scp.add_supported_context(StudyRootQueryRetrieveInformationModelFind)
    server = scp.start_server(
        ("127.0.0.1", 0),
        block=False,
        evt_handlers=[(evt.EVT_C_STORE, on_store), (evt.EVT_C_FIND, on_find)],
    )
    try:
        port = server.socket.getsockname()[1]
        dest = Destination("127.0.0.1", port, "RESEARCHPACS")
        sender = DicomSender(dest)
        sent = result_object(salt)
        unsupported = result_object(salt)
        unsupported.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"  # CT: the SCP does not accept it
        assert sender.send([sent, unsupported]) == [True, False]
        assert [ds.SOPInstanceUID for ds in received] == [sent.SOPInstanceUID]

        state = MemoryState()
        state.mark(
            ("E1", "qc.dcm:1"),
            dest.name,
            SENT,
            study_uid=STUDY,
            series_uid=str(sent.SeriesInstanceUID),
        )
        state.mark(("E9", "x.dcm:1"), dest.name, SENT, study_uid="2.25.404", series_uid="2.25.4")
        report = verify(state, sender, dest)
        assert report["studies_sent"] == 2 and report["studies_at_destination"] == 1
        assert report["missing"] == ["E9"]
    finally:
        server.shutdown()
    with pytest.raises(DestinationUnavailable):
        DicomSender(Destination("127.0.0.1", port, "RESEARCHPACS")).send([sent])
