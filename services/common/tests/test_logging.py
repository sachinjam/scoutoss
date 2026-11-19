"""Log records must be JSON with the mandatory fields and contain no PHI."""

from __future__ import annotations

import io
import json
import logging

import structlog
from opentelemetry.sdk.trace import TracerProvider

from scoutoss_common.logging import configure_logging, get_logger

FIXTURE_PHI = {
    "name": "Doe^John",
    "mrn": "MRN100001",
    "ssn": "123-45-6789",
    "dob": "19500314",
    "phone": "555-867-5309",
}


def _capture(level: str = "DEBUG") -> io.StringIO:
    stream = io.StringIO()
    configure_logging("test-svc", env="test", level=level, stream=stream)
    return stream


def _records(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_mandatory_fields_present() -> None:
    stream = _capture()
    get_logger("t").info("hello", endpoint="/x", status=200, latency_ms=1.5, user_id="u_abc")
    rec = _records(stream)[-1]
    for key in ("ts", "level", "service", "env", "trace_id", "msg"):
        assert key in rec
    assert rec["service"] == "test-svc"
    assert rec["env"] == "test"
    assert rec["msg"] == "hello"
    assert rec["level"] == "info"


def test_log_records_are_phi_free() -> None:
    stream = _capture()
    log = get_logger("t")
    log.info("patient Doe^John arrived, SSN 123-45-6789", patient_name=FIXTURE_PHI["name"])
    log.warning("lookup", patient_id=FIXTURE_PHI["mrn"], PatientBirthDate=FIXTURE_PHI["dob"])
    log.error("callback", detail=f"call {FIXTURE_PHI['phone']} re MRN: {FIXTURE_PHI['mrn']}")
    log.info("payload", dataset={"PatientName": "Doe^John"}, hl7="PID|1||MRN100001")
    logging.getLogger("stdlib").warning("stdlib says SSN 123-45-6789")
    output = stream.getvalue()
    for value in FIXTURE_PHI.values():
        assert value not in output, value
    assert len(_records(stream)) == 5


def test_uids_survive_scrubbing() -> None:
    stream = _capture()
    uid = "1.2.826.0.1.3680043.10.1234.20231004123456"
    get_logger("t").info("indexed", series_uid=uid, count=12)
    rec = _records(stream)[-1]
    assert rec["series_uid"] == uid
    assert rec["count"] == 12


def test_level_filtering_and_trace_id() -> None:
    stream = _capture("WARNING")
    log = get_logger("t")
    log.info("hidden")
    tracer = TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("span"):
        log.warning("visible")
    recs = _records(stream)
    assert [r["msg"] for r in recs] == ["visible"]
    assert isinstance(recs[0]["trace_id"], str) and len(recs[0]["trace_id"]) == 32


def test_contextvars_are_merged() -> None:
    stream = _capture()
    structlog.contextvars.bind_contextvars(user_id="u_123", endpoint="/query")
    try:
        get_logger("t").info("ctx")
    finally:
        structlog.contextvars.clear_contextvars()
    rec = _records(stream)[-1]
    assert rec["user_id"] == "u_123"
    assert rec["endpoint"] == "/query"


def test_bad_level_falls_back_to_info() -> None:
    stream = _capture("NOPE")
    get_logger("t").debug("dbg")
    get_logger("t").info("inf")
    assert [r["msg"] for r in _records(stream)] == ["inf"]
