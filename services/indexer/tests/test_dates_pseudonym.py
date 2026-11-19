from __future__ import annotations

from datetime import date

import pytest

from scoutoss_indexer.deid.dates import (
    OFFSET_WINDOW,
    age_years,
    cap_age,
    dicom_age,
    parse_da,
    parse_dicom_age,
    random_offset_days,
    shift_da,
    shift_dt,
)
from scoutoss_indexer.deid.pseudonym import hash_uid, is_pseudonym, pseudonymize, site_code


def test_pseudonym_is_deterministic_and_keyed(salt: bytes) -> None:
    a = pseudonymize("MRN100001", salt)
    assert a == pseudonymize(" mrn100001 ", salt)  # normalized
    assert a != pseudonymize("MRN100002", salt)
    assert a != pseudonymize("MRN100001", b"other-salt")
    assert is_pseudonym(a) and len(a) == 26 and "MRN" not in a
    assert (
        not is_pseudonym("MRN100001")
        and not is_pseudonym(None)
        and not is_pseudonym("SP" + "Z" * 24)
    )
    with pytest.raises(ValueError, match="empty"):
        pseudonymize("  ", salt)
    with pytest.raises(ValueError, match="salt"):
        pseudonymize("x", b"")


def test_hash_uid(salt: bytes) -> None:
    uid = "1.2.826.0.1.3680043.10.1234.99"
    h = hash_uid(uid, salt)
    assert h.startswith("2.25.") and len(h) <= 64 and h == hash_uid(uid, salt)
    assert h != hash_uid(uid + "1", salt)
    assert hash_uid("1.2.840.10008.1.2.1", salt) == "1.2.840.10008.1.2.1"


def test_site_code(salt: bytes) -> None:
    code = site_code("Springfield General Hospital", salt)
    assert code is not None and code.startswith("SITE-") and "Springfield" not in code
    assert site_code("springfield general hospital ", salt) == code
    assert site_code(None, salt) is None and site_code(" ", salt) is None


def test_offsets_within_window() -> None:
    offsets = {random_offset_days() for _ in range(300)}
    assert all(OFFSET_WINDOW[0] <= o <= OFFSET_WINDOW[1] for o in offsets)
    assert len(offsets) > 50
    assert random_offset_days((-5, -5)) == -5
    with pytest.raises(ValueError, match="window"):
        random_offset_days((1, 0))


def test_shift_preserves_order_and_intervals() -> None:
    dates = ["20220110", "20220415", "20230101", "19991231"]
    offset = -137
    shifted = [shift_da(d, offset) for d in dates]
    parsed = [parse_da(d) for d in dates]
    shifted_parsed = [parse_da(s) for s in shifted]
    assert all(s is not None for s in shifted_parsed)
    assert sorted(range(4), key=lambda i: parsed[i]) == sorted(
        range(4), key=lambda i: shifted_parsed[i]
    )
    for p, s in zip(parsed, shifted_parsed, strict=True):
        assert p is not None and s is not None and (s - p).days == offset
    assert (shifted_parsed[1] - shifted_parsed[0]) == (parsed[1] - parsed[0])  # type: ignore[operator]


def test_shift_edge_cases() -> None:
    assert shift_da("", -10) == ""
    assert shift_da(None, -10) is None
    assert shift_da("garbage", -10) == ""  # never passes an unparseable date through
    assert shift_da("2022.01.10", -10) == "20211231"
    assert shift_dt("20220110093000.123+0100", -10) == "20211231093000.123+0100"
    assert shift_dt("", -1) == ""
    assert shift_dt("bad!", -1) == ""
    assert parse_da("20220230") is None and parse_da(None) is None


def test_ages() -> None:
    assert age_years(date(1950, 3, 14), date(2022, 3, 13)) == pytest.approx(71.997, abs=0.01)
    assert age_years(date(2000, 2, 29), date(2001, 3, 1)) >= 1.0
    with pytest.raises(ValueError, match="precedes"):
        age_years(date(2020, 1, 1), date(2019, 1, 1))
    assert dicom_age(date(1950, 3, 14), date(2022, 1, 10)) == "071Y"
    assert dicom_age(date(1920, 1, 1), date(2022, 1, 10)) == "090Y"
    assert dicom_age(date(2021, 1, 1), date(2022, 1, 10)) == "012M"
    assert dicom_age(date(2022, 1, 1), date(2022, 1, 10)) == "009D"
    assert parse_dicom_age("063Y") == 63.0
    assert parse_dicom_age("006M") == 0.5
    assert parse_dicom_age("095Y") == 90.0
    assert parse_dicom_age("14") == 14.0
    assert parse_dicom_age("abc") is None and parse_dicom_age(None) is None
    assert parse_dicom_age("002W") == pytest.approx(0.04, abs=0.01)
    assert cap_age(None) is None and cap_age(89.9) == 89.9
