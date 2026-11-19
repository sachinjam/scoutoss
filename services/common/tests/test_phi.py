from __future__ import annotations

import pytest

from scoutoss_common.phi import REDACTED, contains_phi, redact_text, scrub_mapping, scrub_value

PHI_SAMPLES = [
    ("John Doe SSN 123-45-6789", ["John Doe", "123-45-6789"]),
    ("patient Jane Roe was seen", ["Jane Roe"]),
    ("Contact jdoe@example.org or (555) 867-5309", ["jdoe@example.org", "867-5309"]),
    ("MRN: 00123456 admitted", ["00123456"]),
    ("DOB 1950-03-14", ["1950-03-14"]),
    ("born 03/14/1950", ["03/14/1950"]),
    ("Doe^John^Q study", ["Doe^John"]),
    ("Seen by Dr. Gregory House", ["Gregory House"]),
    ("Lives at 742 Evergreen Terrace Drive", ["742 Evergreen Terrace Drive"]),
    ("Springfield IL 62704", ["62704"]),
]


@pytest.mark.parametrize(("text", "secrets"), PHI_SAMPLES)
def test_redacts_phi(text: str, secrets: list[str]) -> None:
    out = redact_text(text)
    for s in secrets:
        assert s not in out
    assert contains_phi(text)


@pytest.mark.parametrize(
    "text",
    [
        "how many CT studies",
        "patients under 85 with lung cancer AND CT chest",
        "Show Lung Cancer patients by Body Part",
        "Siemens Healthineers scanners in 2021",
        "CT CHEST W/O CONTRAST",
        "Chest Abdomen Pelvis",
        "series 1.2.826.0.1.3680043.10.1234.20231004123456 ok",
    ],
)
def test_keeps_clinical_queries(text: str) -> None:
    assert redact_text(text) == text


def test_allow_terms_and_options() -> None:
    assert (
        redact_text("Acme Zentrix scanners", allow_terms=["acme zentrix"])
        == "Acme Zentrix scanners"
    )
    assert "[NAME]" in redact_text("Acme Zentrix scanners")
    assert redact_text("on 2021-02-03", redact_dates=False) == "on 2021-02-03"
    assert redact_text("Bob Smith", detect_names=False) == "Bob Smith"
    assert redact_text("") == ""


def test_scrub_mapping_drops_phi_keys_recursively() -> None:
    data = {
        "patient_name": "Doe^John",
        "nested": {"PatientID": "MRN1", "note": "SSN 123-45-6789", "items": ["555-867-5309", 3]},
        "blob": b"\x00\x01",
        "count": 4,
    }
    clean = scrub_mapping(data)
    assert clean["patient_name"] == REDACTED
    assert clean["nested"]["PatientID"] == REDACTED
    assert "123-45-6789" not in clean["nested"]["note"]
    assert clean["nested"]["items"][0] == "[PHONE]"
    assert clean["blob"] == "<2 bytes>"
    assert clean["count"] == 4


def test_scrub_value_depth_limit() -> None:
    deep: dict[str, object] = {"v": "x"}
    for _ in range(10):
        deep = {"d": deep}
    assert REDACTED in str(scrub_value(deep))
