"""Encrypted store for the only re-identification material ScoutOSS keeps.

* ``deid.subject_map``   pseudonym -> original PatientID (AES-256-GCM ciphertext)
* ``deid.project_offset`` project  -> date offset in days (AES-256-GCM ciphertext)

Ciphertexts are bound to their row key via GCM associated data, so a ciphertext copied
into another row fails to decrypt. The ``deid`` schema is owned by the ``deid_svc``
database role; application roles have no privileges on it (see infra/postgres/init.sql).
"""

from __future__ import annotations

import os
import threading
from typing import Protocol

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from scoutoss_indexer.deid.dates import OFFSET_WINDOW, random_offset_days

DDL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'deid') THEN
        CREATE SCHEMA deid;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS deid.subject_map (
    pseudonym      text PRIMARY KEY,
    project        text NOT NULL,
    patient_id_enc bytea NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS deid.project_offset (
    project     text PRIMARY KEY,
    offset_enc  bytea NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
"""


class Vault(Protocol):
    def project_offset(self, project: str) -> int: ...

    def record_subject(self, pseudonym: str, patient_id: str, project: str) -> None: ...


class Cipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("vault key must be 32 bytes")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, aad: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, plaintext.encode(), aad.encode())

    def decrypt(self, blob: bytes, aad: str) -> str:
        return self._aead.decrypt(blob[:12], blob[12:], aad.encode()).decode()


class MemoryVault:
    """In-process vault for tests and the offline folder indexer."""

    def __init__(self, offsets: dict[str, int] | None = None) -> None:
        self.offsets = dict(offsets or {})
        self.subjects: dict[str, tuple[str, str]] = {}

    def project_offset(self, project: str) -> int:
        if project not in self.offsets:
            self.offsets[project] = random_offset_days(OFFSET_WINDOW)
        return self.offsets[project]

    def record_subject(self, pseudonym: str, patient_id: str, project: str) -> None:
        self.subjects.setdefault(pseudonym, (patient_id, project))


class PostgresVault:
    def __init__(self, conninfo: str, key: bytes) -> None:
        self.conninfo = conninfo
        self.cipher = Cipher(key)
        self._offsets: dict[str, int] = {}
        self._known: set[str] = set()
        self._lock = threading.Lock()

    def migrate(self) -> None:
        with psycopg.connect(self.conninfo, autocommit=True) as conn:
            conn.execute(DDL)

    def project_offset(self, project: str) -> int:
        with self._lock:
            if project in self._offsets:
                return self._offsets[project]
            with psycopg.connect(self.conninfo) as conn:
                row = conn.execute(
                    "SELECT offset_enc FROM deid.project_offset WHERE project = %s", (project,)
                ).fetchone()
                if row is None:
                    candidate = random_offset_days(OFFSET_WINDOW)
                    conn.execute(
                        "INSERT INTO deid.project_offset (project, offset_enc) VALUES (%s, %s) "
                        "ON CONFLICT (project) DO NOTHING",
                        (project, self.cipher.encrypt(str(candidate), f"offset:{project}")),
                    )
                    row = conn.execute(
                        "SELECT offset_enc FROM deid.project_offset WHERE project = %s",
                        (project,),
                    ).fetchone()
                assert row is not None
                offset = int(self.cipher.decrypt(bytes(row[0]), f"offset:{project}"))
            self._offsets[project] = offset
            return offset

    def record_subject(self, pseudonym: str, patient_id: str, project: str) -> None:
        if pseudonym in self._known:
            return
        with psycopg.connect(self.conninfo) as conn:
            conn.execute(
                "INSERT INTO deid.subject_map (pseudonym, project, patient_id_enc) "
                "VALUES (%s, %s, %s) ON CONFLICT (pseudonym) DO NOTHING",
                (pseudonym, project, self.cipher.encrypt(patient_id, f"subject:{pseudonym}")),
            )
        self._known.add(pseudonym)

    def reveal(self, pseudonym: str) -> str | None:
        """Re-identify one pseudonym (honest-broker use only; never called by services)."""
        with psycopg.connect(self.conninfo) as conn:
            row = conn.execute(
                "SELECT patient_id_enc FROM deid.subject_map WHERE pseudonym = %s", (pseudonym,)
            ).fetchone()
        if row is None:
            return None
        return self.cipher.decrypt(bytes(row[0]), f"subject:{pseudonym}")
