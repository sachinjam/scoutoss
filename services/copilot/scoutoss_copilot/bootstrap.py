"""Wire real dependencies for the running copilot service."""

from __future__ import annotations

from opensearchpy import OpenSearch
from psycopg_pool import ConnectionPool

from scoutoss_common.audit import AuditLogger
from scoutoss_common.auth import JWTValidator
from scoutoss_copilot import SERVICE_NAME
from scoutoss_copilot.app import CopilotDeps
from scoutoss_copilot.clients import SchemaClient, ServiceToken, XnatPipelines
from scoutoss_copilot.cohorts import ConversationMemory, PostgresCohortRepo
from scoutoss_copilot.executor import Executor
from scoutoss_copilot.llm import build_provider
from scoutoss_copilot.service import Copilot
from scoutoss_copilot.settings import CopilotSettings, get_settings


def build_deps(settings: CopilotSettings | None = None) -> CopilotDeps:
    settings = settings or get_settings()
    auth = (
        (settings.opensearch_user, settings.opensearch_password)
        if settings.opensearch_user
        else None
    )
    search = OpenSearch(hosts=[settings.opensearch_url], http_auth=auth, timeout=10, max_retries=2)
    pool = ConnectionPool(settings.database_url, min_size=1, max_size=5, open=True)
    repo = PostgresCohortRepo(pool)
    repo.migrate()
    secret = settings.secret("client")
    token = ServiceToken(settings.token_url, settings.client_id, secret) if secret else None
    copilot = Copilot(
        llm=build_provider(settings),
        executor=Executor(
            search, timeout=settings.query_timeout_seconds, max_rows=settings.max_rows
        ),
        cohorts=repo,
        memory=ConversationMemory(turns=settings.history_turns),
        schema=SchemaClient(settings.indexer_url, token),
        max_rows=settings.max_rows,
    )
    xnat_password = settings.secret("xnat")
    pipelines = (
        XnatPipelines(settings.xnat_url, settings.xnat_user, xnat_password)
        if xnat_password
        else None
    )

    def postgres() -> bool:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return True

    return CopilotDeps(
        copilot=copilot,
        validator=JWTValidator(
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            jwks_url=settings.auth_jwks_url,
        ),
        audit=AuditLogger(search, service=SERVICE_NAME),
        search=search,
        pipelines=pipelines,
        max_pipeline_sessions=settings.max_pipeline_sessions,
        ready_checks=(postgres,),
    )
