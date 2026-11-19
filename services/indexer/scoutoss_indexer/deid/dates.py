"""DICOM date handling: parsing, per-project shifting and age-at-study.

A project gets ONE random offset (stored encrypted in the vault), applied to every date
of every patient in it. Intervals between a patient's studies, and the order of studies
across patients, are therefore preserved, while absolute dates are not.
"""

from __future__ import annotations

import re
import secrets
from datetime import date, timedelta

# Offsets are always negative (dates move into the past) and at least a month.
OFFSET_WINDOW = (-730, -30)
HIPAA_AGE_CAP = 90  # ages over 89 are reported as 90 (HIPAA Safe Harbor)

_DA = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DT = re.compile(r"^(\d{8})(.*)$")


def random_offset_days(window: tuple[int, int] = OFFSET_WINDOW) -> int:
    low, high = window
    if low > high:
        raise ValueError("invalid window")
    return low + secrets.randbelow(high - low + 1)


def parse_da(value: str | None) -> date | None:
    """Parse DICOM DA (``YYYYMMDD``; tolerates ``YYYY.MM.DD`` / ``YYYY-MM-DD``)."""
    if not value:
        return None
    cleaned = str(value).strip().replace(".", "").replace("-", "")
    m = _DA.match(cleaned)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def format_da(value: date) -> str:
    return value.strftime("%Y%m%d")


def shift_da(value: str | None, offset_days: int) -> str | None:
    """Shift a DA string; unparseable values are blanked (never passed through)."""
    if value is None or str(value).strip() == "":
        return value
    parsed = parse_da(value)
    if parsed is None:
        return ""
    return format_da(parsed + timedelta(days=offset_days))


def shift_dt(value: str | None, offset_days: int) -> str | None:
    """Shift the date part of a DICOM DT, keeping the time and timezone suffix."""
    if value is None or str(value).strip() == "":
        return value
    m = _DT.match(str(value).strip())
    if not m:
        return ""
    shifted = shift_da(m.group(1), offset_days)
    return f"{shifted}{m.group(2)}" if shifted else ""


def age_years(birth: date, at: date) -> float:
    if at < birth:
        raise ValueError("date precedes birth date")
    years = at.year - birth.year - ((at.month, at.day) < (birth.month, birth.day))
    anniversary = _safe_replace_year(birth, birth.year + years)
    next_anniversary = _safe_replace_year(birth, birth.year + years + 1)
    fraction = (at - anniversary).days / max((next_anniversary - anniversary).days, 1)
    return round(years + fraction, 2)


def _safe_replace_year(d: date, year: int) -> date:
    try:
        return d.replace(year=year)
    except ValueError:  # 29 Feb in a non-leap year
        return d.replace(year=year, day=28)


def cap_age(age: float | None) -> float | None:
    if age is None:
        return None
    return float(HIPAA_AGE_CAP) if age >= HIPAA_AGE_CAP else age


def dicom_age(birth: date, at: date) -> str:
    """DICOM AS string (``063Y``, ``011M``, ``020D``), capped at ``090Y``."""
    years = int(age_years(birth, at))
    if years >= HIPAA_AGE_CAP:
        return f"{HIPAA_AGE_CAP:03d}Y"
    if years >= 2:
        return f"{years:03d}Y"
    days = (at - birth).days
    if days >= 60:
        months = (at.year - birth.year) * 12 + at.month - birth.month - (at.day < birth.day)
        return f"{months:03d}M"
    return f"{days:03d}D"


def parse_dicom_age(value: str | None) -> float | None:
    """Parse DICOM AS (``nnnD|W|M|Y``) into years; capped per HIPAA."""
    if not value:
        return None
    text = str(value).strip().upper()
    m = re.match(r"^(\d{1,3})\s*([DWMY]?)$", text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "Y"
    factor = {"D": 1 / 365.25, "W": 7 / 365.25, "M": 1 / 12, "Y": 1.0}[unit]
    return cap_age(round(n * factor, 2))
