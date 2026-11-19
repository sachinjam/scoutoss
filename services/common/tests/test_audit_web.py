from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from scoutoss_common.audit import AuditError, AuditLogger, params_hash
from scoutoss_common.metrics import HTTP_REQUESTS
from scoutoss_common.web import client_ip, install_observability
from tests.support.fake_opensearch import FakeOpenSearch


def test_audit_record_is_append_only_and_hashes_params(fake_os: FakeOpenSearch) -> None:
    audit = AuditLogger(fake_os, service="svc")
    doc = audit.record(
        user="u_1",
        action="query",
        resource="dicom_studies",
        params={"q": "John Doe SSN 123-45-6789"},
        ip="10.0.0.1",
    )
    assert doc["params_hash"] == params_hash({"q": "John Doe SSN 123-45-6789"})
    stored = fake_os.docs("audit")
    assert len(stored) == 1
    assert "John" not in str(stored[0]) and "123-45" not in str(stored[0])
    assert set(stored[0]) == {
        "user",
        "action",
        "resource",
        "params_hash",
        "ts",
        "ip",
        "service",
        "trace_id",
        "outcome",
    }
    _, kwargs = fake_os.calls[-1]
    assert kwargs["op_type"] == "create"


def test_audit_rejects_unknown_action(fake_os: FakeOpenSearch) -> None:
    with pytest.raises(ValueError, match="unknown audit action"):
        AuditLogger(fake_os, service="svc").record(user="u", action="drop", resource="x")


def test_audit_fails_closed(fake_os: FakeOpenSearch) -> None:
    fake_os.fail_index.add("audit")
    with pytest.raises(AuditError):
        AuditLogger(fake_os, service="svc").record(user="u", action="export", resource="c1")


def test_params_hash_is_canonical() -> None:
    assert params_hash({"a": 1, "b": 2}) == params_hash({"b": 2, "a": 1})
    assert params_hash(None) != params_hash({})


def test_request_log_carries_user_pseudonym() -> None:
    import io
    import json

    from scoutoss_common.auth import Principal
    from scoutoss_common.logging import configure_logging

    stream = io.StringIO()
    configure_logging("websvc", env="test", stream=stream)
    app = FastAPI()
    install_observability(app, "websvc")

    @app.get("/who")
    def who(request: Request) -> dict[str, str]:
        request.state.principal = Principal(subject="abc", username="u")
        return {"ok": "1"}

    TestClient(app).get("/who")
    rec = [json.loads(line) for line in stream.getvalue().splitlines() if '"request"' in line][-1]
    assert rec["user_id"].startswith("u_") and rec["endpoint"] == "/who" and rec["status"] == 200
    assert "latency_ms" in rec and "abc" not in json.dumps(rec)


def test_middleware_metrics_and_request_id() -> None:
    app = FastAPI()
    install_observability(app, "websvc")

    @app.get("/items/{item_id}")
    def item(item_id: int, request: Request) -> dict[str, object]:
        return {"id": item_id, "ip": client_ip(request)}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("kaboom")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/items/7", headers={"x-request-id": "rid-1", "x-forwarded-for": "1.2.3.4, 5.6.7.8"}
    )
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == "rid-1"
    assert resp.json()["ip"] == "1.2.3.4"
    assert client.get("/boom").status_code == 500
    assert client.get("/nowhere").status_code == 404
    value = HTTP_REQUESTS.labels("websvc", "GET", "/items/{item_id}", "200")._value.get()
    assert value >= 1
    assert HTTP_REQUESTS.labels("websvc", "GET", "/boom", "500")._value.get() >= 1
    metrics = client.get("/metrics").text
    assert "http_requests_total" in metrics and "http_request_duration_seconds_bucket" in metrics


def test_tracing_setup_with_memory_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from scoutoss_common.tracing import current_trace_id, setup_tracing

    exporter = InMemorySpanExporter()
    app = FastAPI()

    @app.get("/t")
    def t() -> dict[str, str | None]:
        return {"trace": current_trace_id()}

    provider = setup_tracing("trace-test", app, exporter=exporter)
    assert provider is not None
    client = TestClient(app)
    parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    resp = client.get("/t", headers={"traceparent": parent})
    assert resp.json()["trace"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    provider.force_flush()
    assert any(
        s.context.trace_id == int("4bf92f3577b34da6a3ce929d0e0e4736", 16)
        for s in exporter.get_finished_spans()
    )
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert setup_tracing("disabled") is None
