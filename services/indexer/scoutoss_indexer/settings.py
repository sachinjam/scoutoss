"""Indexer configuration, read from environment variables (and ``*_FILE`` secrets)."""

from __future__ import annotations

import base64
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def read_secret(value: str | None, file_var: str) -> str | None:
    """Prefer ``<NAME>_FILE`` (mounted secret) over the plain env value."""
    path = os.getenv(file_var)
    if path and Path(path).is_file():
        return Path(path).read_text().strip()
    return value


class IndexerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    env: str = Field("dev", alias="ENV")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    opensearch_url: str = Field("http://opensearch:9200", alias="OPENSEARCH_URL")
    opensearch_user: str | None = Field(None, alias="OPENSEARCH_USER")
    opensearch_password: str | None = Field(None, alias="OPENSEARCH_PASSWORD")
    opensearch_verify_certs: bool = Field(True, alias="OPENSEARCH_VERIFY_CERTS")

    database_url: str = Field(
        "postgresql://scout_app:scout_app@scout-db:5432/scoutoss", alias="DATABASE_URL"
    )
    deid_database_url: str | None = Field(None, alias="DEID_DATABASE_URL")

    source: Literal["xnat", "folder", "none"] = Field("xnat", alias="INDEXER_SOURCE")
    folder_path: Path = Field(Path("/data/dicom"), alias="INDEXER_FOLDER")
    poll_seconds: float = Field(10.0, alias="INDEXER_POLL_SECONDS")
    worker_enabled: bool = Field(True, alias="INDEXER_WORKER_ENABLED")
    batch_size: int = Field(200, alias="INDEXER_BATCH_SIZE")
    max_attempts: int = Field(5, alias="INDEXER_MAX_ATTEMPTS")

    xnat_url: str = Field("http://xnat-web:8080", alias="XNAT_URL")
    xnat_public_url: str = Field("https://xnat.localhost:24443", alias="XNAT_PUBLIC_URL")
    xnat_user: str = Field("scout_indexer", alias="XNAT_USER")
    xnat_password: str | None = Field(None, alias="XNAT_PASSWORD")

    hmac_salt: str | None = Field(None, alias="DEID_HMAC_SALT")
    vault_key: str | None = Field(None, alias="DEID_VAULT_KEY")
    webhook_token: str | None = Field(None, alias="INDEXER_WEBHOOK_TOKEN")

    auth_issuer: str = Field("https://auth.localhost:24443/realms/scoutoss", alias="AUTH_ISSUER")
    auth_audience: str = Field("scoutoss-indexer", alias="AUTH_AUDIENCE")
    auth_jwks_url: str = Field(
        "http://keycloak:8080/realms/scoutoss/protocol/openid-connect/certs",
        alias="AUTH_JWKS_URL",
    )

    @field_validator("poll_seconds")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("INDEXER_POLL_SECONDS must be > 0")
        return v

    def salt_bytes(self) -> bytes:
        salt = read_secret(self.hmac_salt, "DEID_HMAC_SALT_FILE")
        if not salt:
            raise RuntimeError("DEID_HMAC_SALT (or DEID_HMAC_SALT_FILE) is required")
        return salt.encode()

    def vault_key_bytes(self) -> bytes:
        key = read_secret(self.vault_key, "DEID_VAULT_KEY_FILE")
        if not key:
            raise RuntimeError("DEID_VAULT_KEY (or DEID_VAULT_KEY_FILE) is required")
        raw = base64.b64decode(key)
        if len(raw) != 32:
            raise RuntimeError("DEID_VAULT_KEY must be 32 bytes, base64-encoded")
        return raw

    def xnat_secret(self) -> str:
        return read_secret(self.xnat_password, "XNAT_PASSWORD_FILE") or ""

    def webhook_secret(self) -> str | None:
        return read_secret(self.webhook_token, "INDEXER_WEBHOOK_TOKEN_FILE")


@lru_cache(maxsize=1)
def get_settings() -> IndexerSettings:
    return IndexerSettings()
