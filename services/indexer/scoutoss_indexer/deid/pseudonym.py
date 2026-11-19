"""Keyed (HMAC-SHA256) pseudonyms.

The same site salt is used by the DICOM gateway and the HL7 clinical ingest, so imaging
and clinical records for one patient join on the same pseudonym. Without the salt the
mapping cannot be recomputed or brute-forced from a list of MRNs.
"""

from __future__ import annotations

import hashlib
import hmac

PSEUDONYM_PREFIX = "SP"
_STANDARD_UID_ROOT = "1.2.840.10008."


def _digest(salt: bytes, value: str, purpose: str) -> bytes:
    if not salt:
        raise ValueError("salt must not be empty")
    return hmac.new(salt, f"{purpose}:{value}".encode(), hashlib.sha256).digest()


def pseudonymize(patient_id: str, salt: bytes) -> str:
    """Deterministic patient pseudonym, e.g. ``SP7F3A...`` (96 bits, hex)."""
    normalized = patient_id.strip().upper()
    if not normalized:
        raise ValueError("patient_id is empty")
    return PSEUDONYM_PREFIX + _digest(salt, normalized, "patient").hex()[:24].upper()


def is_pseudonym(value: str | None) -> bool:
    if not value or not value.startswith(PSEUDONYM_PREFIX) or len(value) != 26:
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def hash_uid(uid: str, salt: bytes) -> str:
    """Replace a UID with a deterministic ``2.25.<128-bit int>`` UID.

    DICOM-defined UIDs (transfer syntaxes, SOP classes) are returned unchanged.
    """
    uid = uid.strip().rstrip("\x00")
    if uid.startswith(_STANDARD_UID_ROOT):
        return uid
    value = int.from_bytes(_digest(salt, uid, "uid")[:16], "big")
    return f"2.25.{value}"


def site_code(institution: str | None, salt: bytes) -> str | None:
    """Coarse, non-reversible site label in place of the institution name."""
    if not institution or not institution.strip():
        return None
    return "SITE-" + _digest(salt, institution.strip().upper(), "site").hex()[:6].upper()


def short_hash(value: str, salt: bytes, length: int = 8) -> str:
    return _digest(salt, value, "label").hex()[:length].upper()
