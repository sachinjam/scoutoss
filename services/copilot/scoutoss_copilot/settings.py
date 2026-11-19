"""Copilot configuration (environment variables; secrets may come from ``*_FILE``)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _secret(value: str | None, file_var: str) -> str | None:
    path = os.getenv(file_var)
    if path and Path(path).is_file():
        return Path(path).read_text().strip()
    return value


class CopilotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    env: str = Field("dev", alias="ENV")
    opensearch_url: str = Field("http://opensearch:9200", alias="OPENSEARCH_URL")
    opensearch_user: str | None = Field(None, alias="OPENSEARCH_USER")
    opensearch_password: str | None = Field(None, alias="OPENSEARCH_PASSWORD")
    database_url: str = Field(
        "postgresql://copilot_app:copilot_app@scout-db:5432/scoutoss", alias="DATABASE_URL"
    )

    llm_provider: Literal["fake", "heuristic", "azure", "openai_compatible"] = Field(
        "heuristic", alias="LLM_PROVIDER"
    )
    llm_model: str = Field("gpt-4o", alias="LLM_MODEL")
    llm_base_url: str | None = Field(None, alias="LLM_BASE_URL")
    llm_api_key: str | None = Field(None, alias="LLM_API_KEY")
    llm_timeout: float = Field(30.0, alias="LLM_TIMEOUT_SECONDS")
    azure_endpoint: str | None = Field(None, alias="AZURE_OPENAI_ENDPOINT")
    azure_deployment: str = Field("gpt-4o", alias="AZURE_OPENAI_DEPLOYMENT")
    azure_api_version: str = Field("2024-10-21", alias="AZURE_OPENAI_API_VERSION")
    azure_api_key: str | None = Field(None, alias="AZURE_OPENAI_API_KEY")

    query_timeout_seconds: float = Field(5.0, alias="QUERY_TIMEOUT_SECONDS")
    max_rows: int = Field(10_000, alias="QUERY_MAX_ROWS")
    history_turns: int = Field(6, alias="COPILOT_HISTORY_TURNS")

    indexer_url: str = Field("http://indexer:8000", alias="INDEXER_URL")
    token_url: str = Field(
        "http://keycloak:8080/realms/scoutoss/protocol/openid-connect/token", alias="TOKEN_URL"
    )
    client_id: str = Field("scoutoss-copilot", alias="CLIENT_ID")
    client_secret: str | None = Field(None, alias="CLIENT_SECRET")

    auth_issuer: str = Field("https://auth.localhost:24443/realms/scoutoss", alias="AUTH_ISSUER")
    auth_audience: str = Field("scoutoss-copilot", alias="AUTH_AUDIENCE")
    auth_jwks_url: str = Field(
        "http://keycloak:8080/realms/scoutoss/protocol/openid-connect/certs", alias="AUTH_JWKS_URL"
    )

    xnat_url: str = Field("http://xnat-web:8080", alias="XNAT_URL")
    xnat_user: str = Field("scout_pipeline", alias="XNAT_PIPELINE_USER")
    xnat_password: str | None = Field(None, alias="XNAT_PIPELINE_PASSWORD")
    max_pipeline_sessions: int = Field(200, alias="MAX_PIPELINE_SESSIONS")

    def secret(self, name: str) -> str | None:
        value = {
            "client": self.client_secret,
            "llm": self.llm_api_key,
            "azure": self.azure_api_key,
            "xnat": self.xnat_password,
        }[name]
        file_var = {
            "client": "CLIENT_SECRET_FILE",
            "llm": "LLM_API_KEY_FILE",
            "azure": "AZURE_OPENAI_API_KEY_FILE",
            "xnat": "XNAT_PIPELINE_PASSWORD_FILE",
        }[name]
        return _secret(value, file_var)


@lru_cache(maxsize=1)
def get_settings() -> CopilotSettings:
    return CopilotSettings()
