"""Generated synthetic cases (``make seed CASES=N``)."""

from __future__ import annotations

from scoutoss_indexer.clinical import categorize
from scoutoss_indexer.deid.deidentify import DeidConfig, Deidentifier
from scoutoss_indexer.synthetic import CONDITIONS, PATIENTS, generate_cases, make_instance


def test_cases_are_deterministic_unique_and_consistent() -> None:
    cases = generate_cases(100)
    again = generate_cases(100)
    assert [c.patient for c in cases] == [c.patient for c in again]
    assert [s.study_uid for c in cases for s in c.studies] == [
        s.study_uid for c in again for s in c.studies
    ]
    mrns = {c.patient.mrn for c in cases}
    assert len(mrns) == 100 and not mrns & {p.mrn for p in PATIENTS}
    studies = [s for c in cases for s in c.studies]
    assert 100 <= len(studies) <= 200
    assert len({s.study_uid for s in studies}) == len(studies)
    assert len({x.series_uid for s in studies for x in s.series}) == sum(
        len(s.series) for s in studies
    )
    for case in cases:
        allowed = {(m, d) for m, d, *_ in case.condition.studies}
        for study in case.studies:
            assert study.patient is case.patient
            assert (study.series[0].modality, study.description) in allowed
            assert study.study_date.year - case.patient.birth_date.year >= 28
    assert len({c.condition.code for c in cases}) >= 6
    assert [c.patient.mrn for c in generate_cases(3, seed=7)] != [c.patient.mrn for c in cases[:3]]


def test_every_condition_maps_to_a_known_category_or_none() -> None:
    known = {c.code: categorize(c.code, c.text) for c in CONDITIONS}
    assert known["C34.90"] == "LUNG_CANCER" and known["I63.9"] == "STROKE"
    assert known["J18.9"] == "PNEUMONIA" and known["C22.0"] == "LIVER_CANCER"


def test_generated_instances_deidentify_cleanly(salt: bytes) -> None:
    case = generate_cases(5)[0]
    study = case.studies[0]
    ds = make_instance(study, study.series[0], 0)
    assert str(ds.PatientID) == case.patient.mrn and str(ds.PatientName) == case.patient.name
    result = Deidentifier(DeidConfig(salt=salt, project="SCOUT_DEMO", offset_days=-45)).apply(ds)
    dumped = str(ds)
    assert case.patient.mrn not in dumped and case.patient.name.split("^")[0] not in dumped
    assert str(ds.PatientID) == result.pseudonym and result.pseudonym.startswith("SP")
    assert str(ds.Modality) == study.series[0].modality
