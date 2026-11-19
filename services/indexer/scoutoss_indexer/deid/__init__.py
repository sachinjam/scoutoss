"""De-identification: HMAC pseudonyms, per-project date shifting, PS3.15 Basic Profile.

XNAT's DicomEdit language has no keyed hash, so ScoutOSS de-identifies *before* XNAT in
the DICOM gateway (``scoutoss_indexer.gateway``) using this package. The project-level
DicomEdit script in ``xnat/anon`` then strips the same tags again as a second layer.
"""

from scoutoss_indexer.deid.dates import (
    age_years,
    dicom_age,
    parse_da,
    shift_da,
    shift_dt,
)
from scoutoss_indexer.deid.deidentify import DeidConfig, Deidentifier, DeidRejected
from scoutoss_indexer.deid.pseudonym import hash_uid, pseudonymize, site_code

__all__ = [
    "DeidConfig",
    "DeidRejected",
    "Deidentifier",
    "age_years",
    "dicom_age",
    "hash_uid",
    "parse_da",
    "pseudonymize",
    "shift_da",
    "shift_dt",
    "site_code",
]
