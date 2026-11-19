"""Worker loop resilience and the XNAT REST source (HTTP mocked with respx)."""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scoutoss_common.catalog import DLQ_INDEX, SERIES_INDEX, STUDIES_INDEX
from scoutoss_indexer.cursor import CursorEntry, MemoryCursor, needs_processing
from scoutoss_indexer.deid.vault import MemoryVault
from scoutoss_indexer.pipeline import Pipeline
from scoutoss_indexer.sources.folder import FolderSource
from scoutoss_indexer.sources.xnat import XnatClient, XnatSource, change_token
from scoutoss_indexer.store import IndexStore
from scoutoss_indexer.worker import Worker
from tests.support.fake_opensearch import FakeOpenSearch

BASE = "http://xnat.test"


def _dicom_bytes(make_ds: Any, **kw: Any) -> bytes:
    buf = io.BytesIO()
    make_ds(**kw).save_as(buf, enforce_file_format=True)
    return buf.getvalue()


@pytest.fixture
def store(fake_os: FakeOpenSearch) -> IndexStore:
    s = IndexStore(fake_os, xnat_public_url="https://xnat.example")  # type: ignore[arg-type]
    s.ensure_indices()
    return s


@pytest.fixture
def pipeline(store: IndexStore, salt: bytes) -> Pipeline:
    return Pipeline(store, salt=salt, vault=MemoryVault({"P1": -30}), default_project="P1")


def _mock_xnat(
    router: respx.MockRouter,
    make_ds: Any,
    *,
    modified: str = "2024-01-01 10:00:00",
    content: bytes | None = None,
) -> bytes:
    content = content or _dicom_bytes(make_ds)
    router.get(f"{BASE}/data/experiments").respond(
        json={
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E1",
                        "label": "S1",
                        "project": "P1",
                        "xsiType": "xnat:ctSessionData",
                        "insert_date": "2024-01-01 09:00:00",
                        "last_modified": modified,
                        "subject_ID": "XNAT_S1",
                    },
                    {
                        "ID": "XNAT_E2",
                        "label": "EMPTY",
                        "project": "P1",
                        "xsiType": "xnat:mrSessionData",
                        "insert_date": "2024-01-01 09:00:00",
                    },
                    {
                        "ID": "XNAT_A1",
                        "label": "assessor",
                        "project": "P1",
                        "xsiType": "xnat:qcAssessmentData",
                    },
                ]
            }
        }
    )
    router.get(f"{BASE}/data/experiments/XNAT_E1/scans").respond(
        json={"ResultSet": {"Result": [{"ID": "1"}, {"ID": "2"}, {"ID": "3"}]}}
    )
    router.get(f"{BASE}/data/experiments/XNAT_E2/scans").respond(json={"ResultSet": {"Result": []}})
    files = [
        {
            "Name": f"img{i}.dcm",
            "URI": f"/data/experiments/XNAT_E1/scans/1/resources/DICOM/files/img{i}.dcm",
        }
        for i in range(4)
    ] + [{"Name": "catalog.xml", "URI": "/x/catalog.xml"}]
    router.get(f"{BASE}/data/experiments/XNAT_E1/scans/1/resources/DICOM/files").respond(
        json={"ResultSet": {"Result": files}}
    )
    router.get(f"{BASE}/data/experiments/XNAT_E1/scans/2/resources/DICOM/files").respond(404)
    router.get(f"{BASE}/data/experiments/XNAT_E1/scans/3/resources/DICOM/files").respond(
        json={"ResultSet": {"Result": [{"Name": "x.dcm", "URI": "/data/bad/x.dcm"}]}}
    )
    router.get(
        url__regex=rf"{BASE}/data/experiments/XNAT_E1/scans/1/resources/DICOM/files/img\d\.dcm"
    ).respond(content=content)
    router.get(f"{BASE}/data/bad/x.dcm").respond(content=b"not dicom at all" * 10)
    router.get(f"{BASE}/data/version").respond(text="1.10")
    return content


def test_xnat_poll_indexes_new_sessions(
    pipeline: Pipeline, fake_os: FakeOpenSearch, make_ds: Any
) -> None:
    cursor = MemoryCursor()
    with respx.mock(assert_all_called=False) as router:
        content = _mock_xnat(router, make_ds)
        client = XnatClient(BASE, "u", "p")
        assert client.ping()
        worker = Worker(pipeline, XnatSource(client, cursor), cursor=cursor, poll_seconds=0.01)
        result = worker.run_once()
        assert result.indexed == 1 and result.dead_lettered == 1
        series = fake_os.docs(SERIES_INDEX)
        assert len(series) == 1 and series[0]["instance_count"] == 4
        assert series[0]["xnat_experiment_id"] == "XNAT_E1" and series[0]["project"] == "P1"
        study = fake_os.docs(STUDIES_INDEX)[0]
        assert "experimentId=XNAT_E1" in study["viewer_url"]
        rows = cursor.get_all()
        assert rows["XNAT_E1"].status == "indexed" and rows["XNAT_E2"].status == "indexed"
        assert "XNAT_A1" not in rows
        # unchanged -> nothing to do; changed token -> reprocessed (idempotently)
        assert worker.run_once().indexed == 0
        _mock_xnat(router, make_ds, modified="2024-02-02 10:00:00", content=content)
        assert worker.run_once().indexed == 1
        assert len(fake_os.docs(SERIES_INDEX)) == 1
        client.close()


def test_xnat_fetch_failures_are_retried_then_dead_lettered(
    pipeline: Pipeline, fake_os: FakeOpenSearch
) -> None:
    cursor = MemoryCursor()
    with respx.mock() as router:
        router.get(f"{BASE}/data/experiments").respond(
            json={
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "XNAT_E9",
                            "project": "P1",
                            "xsiType": "xnat:ctSessionData",
                            "insert_date": "t",
                        }
                    ]
                }
            }
        )
        router.get(f"{BASE}/data/experiments/XNAT_E9/scans").respond(500)
        worker = Worker(
            pipeline,
            XnatSource(XnatClient(BASE, "u", "p"), cursor, max_attempts=2),
            cursor=cursor,
            max_attempts=2,
        )
        worker.run_once()
        assert cursor.get_all()["XNAT_E9"].attempts == 1
        worker.run_once()
        assert cursor.get_all()["XNAT_E9"].attempts == 2
        assert any(d["stage"] == "fetch" for d in fake_os.docs(DLQ_INDEX))
        worker.run_once()  # exhausted: no longer retried
        assert cursor.get_all()["XNAT_E9"].attempts == 2


def test_worker_loop_survives_outages(pipeline: Pipeline) -> None:
    cursor = MemoryCursor()
    with respx.mock() as router:
        router.get(f"{BASE}/data/experiments").mock(side_effect=httpx.ConnectError("down"))
        worker = Worker(
            pipeline,
            XnatSource(XnatClient(BASE, "u", "p"), cursor),
            cursor=cursor,
            poll_seconds=0.01,
        )
        assert worker.run_once_safe() is False
        assert worker.state.last_error == "ConnectError"
        worker.start()
        worker.start()  # idempotent
        time.sleep(0.1)
        assert worker.alive
        worker.trigger({"XNAT_E1"})
        worker.stop()
        assert not worker.alive
        assert cursor.polls["xnat"] >= 1


def test_cursor_failure_inside_error_path(pipeline: Pipeline) -> None:
    class BrokenCursor(MemoryCursor):
        def touch_poll(self, source: str, ok: bool) -> None:
            raise RuntimeError("db down")

    with respx.mock() as router:
        router.get(f"{BASE}/data/experiments").respond(500)
        cursor = BrokenCursor()
        worker = Worker(pipeline, XnatSource(XnatClient(BASE, "u", "p"), cursor), cursor=cursor)
        assert worker.run_once_safe() is False


def test_folder_worker(pipeline: Pipeline, fake_os: FakeOpenSearch, tmp_cohort: Path) -> None:
    source = FolderSource(tmp_cohort, project="P1")
    worker = Worker(pipeline, source)
    result = worker.run_once()
    assert result.indexed == 12
    assert worker.run_once().indexed == 0
    assert source.forget() == 24
    assert FolderSource(tmp_cohort / "missing").pending() == []


def test_cursor_logic() -> None:
    assert needs_processing(None, "t", 5)
    assert needs_processing(CursorEntry("e", "old", "indexed", 0), "new", 5)
    assert not needs_processing(CursorEntry("e", "t", "indexed", 0), "t", 5)
    assert needs_processing(CursorEntry("e", "t", "error", 2), "t", 5)
    assert not needs_processing(CursorEntry("e", "t", "error", 5), "t", 5)
    c = MemoryCursor()
    c.mark("a", project="P", change_token="1", status="indexed")
    c.mark("b", project="Q", change_token="1", status="error")
    assert c.reset(project="P") == 1 and list(c.get_all()) == ["b"]
    assert c.reset(experiment_ids=["b"]) == 1
    assert change_token({"insert_date": "x"}) == "x" and change_token({}) == "0"


def test_worker_wakes_on_trigger(pipeline: Pipeline, tmp_cohort: Path) -> None:
    worker = Worker(pipeline, FolderSource(tmp_cohort, project="P1"), poll_seconds=60)
    worker.start()
    deadline = time.time() + 5
    while worker.state.cycles < 1 and time.time() < deadline:
        time.sleep(0.02)
    worker.trigger()
    while worker.state.cycles < 2 and time.time() < deadline:
        time.sleep(0.02)
    worker.stop()
    assert worker.state.cycles >= 2
    assert threading.active_count() >= 1
