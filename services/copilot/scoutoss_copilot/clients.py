"""Outbound clients: Keycloak client-credentials tokens, the indexer schema endpoint and
the XNAT Container Service (pipeline launches)."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import structlog

from scoutoss_common.catalog import EXAMPLE_VALUE_FIELDS, FIELD_DESCRIPTIONS
from scoutoss_common.xnat_session import XnatSessionAuth

log = structlog.get_logger(__name__)


class ServiceToken:
    """Client-credentials access token for service-to-service calls (cached until expiry)."""

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = httpx.Client(timeout=10, transport=transport)
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 30:
                return self._token
            resp = self.http.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = str(data["access_token"])
            self._expires = time.time() + float(data.get("expires_in", 60))
            return self._token


def static_schema() -> dict[str, Any]:
    return {
        "indices": {
            index: {"fields": fields, "examples": {}, "doc_count": None}
            for index, fields in FIELD_DESCRIPTIONS.items()
        },
        "join_field": "patient_pseudonym",
    }


class SchemaClient:
    """Fetches the live schema (field catalog + de-identified example values) from the
    indexer, falling back to the static catalog. Cached for ``ttl`` seconds. The call is
    traced, so ui -> copilot -> indexer shows up as one trace in Jaeger."""

    def __init__(
        self,
        indexer_url: str,
        token: ServiceToken | None,
        *,
        ttl: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.http = httpx.Client(base_url=indexer_url.rstrip("/"), timeout=5, transport=transport)
        self.token = token
        self.ttl = ttl
        self._cache: tuple[float, dict[str, Any]] | None = None

    def get(self) -> dict[str, Any]:
        if self._cache and time.time() - self._cache[0] < self.ttl:
            return self._cache[1]
        schema = static_schema()
        try:
            headers = {"Authorization": f"Bearer {self.token.get()}"} if self.token else {}
            resp = self.http.get("/schema", headers=headers)
            resp.raise_for_status()
            live = resp.json()
            for index in EXAMPLE_VALUE_FIELDS:
                if index in live.get("indices", {}):
                    schema["indices"][index] = live["indices"][index]
            for index, info in live.get("indices", {}).items():
                if index in schema["indices"]:
                    schema["indices"][index]["doc_count"] = info.get("doc_count")
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("live schema unavailable; using static catalog", error=type(exc).__name__)
        self._cache = (time.time(), schema)
        return schema


class PipelineError(RuntimeError):
    pass


class XnatPipelines:
    """Launch XNAT Container Service wrappers on imaging sessions."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=XnatSessionAuth(base_url, user, password),
            timeout=60,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def wrappers(self, project: str) -> list[dict[str, Any]]:
        """Session-level pipelines enabled in ``project`` (readable by project members;
        the per-wrapper ``/enabled`` endpoint needs owner rights)."""
        resp = self.http.get(
            "/xapi/commands/available",
            params={"project": project, "xsiType": "xnat:imageSessionData"},
        )
        resp.raise_for_status()
        return [
            {
                "id": w["wrapper-id"],
                "name": w["wrapper-name"],
                "label": w.get("wrapper-label") or w["wrapper-name"],
                "description": w.get("wrapper-description", ""),
                "contexts": w.get("wrapper-contexts", []),
                "image": w.get("image-name"),
                "root": w.get("root-element-name", "session"),
            }
            for w in resp.json()
            if w.get("enabled")
        ]

    def launch(self, project: str, wrapper: dict[str, Any], experiment_id: str) -> dict[str, Any]:
        if "xnat:imageSessionData" not in wrapper.get("contexts", []):
            raise PipelineError("pipeline does not run at session level")
        resp = self.http.post(
            f"/xapi/projects/{project}/wrappers/{wrapper['id']}/root/session/launch",
            json={"session": f"/archive/experiments/{experiment_id}"},  # XNAT requires JSON
        )
        if resp.status_code >= 400:
            raise PipelineError(f"launch failed with HTTP {resp.status_code}")
        body: dict[str, Any] = resp.json() if resp.content else {}
        return {
            "experiment_id": experiment_id,
            "status": body.get("status", "submitted"),
            "workflow_id": body.get("workflow-id") or body.get("workflowid"),
            "container_id": body.get("container-id"),
        }
