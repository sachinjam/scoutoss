"""XNAT Container Service client: list a project's session-level pipelines and launch one.
Used by the copilot (run a pipeline on a cohort) and by the indexer's auto-run job."""

from __future__ import annotations

from typing import Any

import httpx

from scoutoss_common.xnat_session import XnatSessionAuth


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

    def sessions(self) -> list[dict[str, Any]]:
        """Image sessions this account can read (``ID``, ``project``, ``xsiType``)."""
        resp = self.http.get(
            "/data/experiments", params={"format": "json", "columns": "ID,project,xsiType"}
        )
        resp.raise_for_status()
        rows: list[dict[str, Any]] = resp.json().get("ResultSet", {}).get("Result", [])
        return [r for r in rows if str(r.get("xsiType", "")).endswith("SessionData")]

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
