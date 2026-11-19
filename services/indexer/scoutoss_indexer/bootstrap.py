"""Wire real dependencies from settings (used by the running service and the CLI)."""

from __future__ import annotations

import os

import psycopg
import structlog
from psycopg_pool import ConnectionPool

from scoutoss_common.audit import AuditLogger
from scoutoss_common.auth import JWTValidator
from scoutoss_indexer import SERVICE_NAME
from scoutoss_indexer.app import IndexerDeps
from scoutoss_indexer.clinical import ClinicalIngestor, PostgresClinicalRepo
from scoutoss_indexer.cursor import PostgresCursor
from scoutoss_indexer.deid.vault import MemoryVault, PostgresVault, Vault
from scoutoss_indexer.pipeline import Pipeline
from scoutoss_indexer.settings import IndexerSettings, get_settings
from scoutoss_indexer.sources.folder import FolderSource
from scoutoss_indexer.sources.xnat import XnatClient, XnatSource
from scoutoss_indexer.store import IndexStore, make_client
from scoutoss_indexer.worker import Worker

log = structlog.get_logger(__name__)


def build_store(settings: IndexerSettings) -> IndexStore:
    client = make_client(
        settings.opensearch_url,
        settings.opensearch_user,
        settings.opensearch_password,
        settings.opensearch_verify_certs,
    )
    return IndexStore(client, xnat_public_url=settings.xnat_public_url)


def build_vault(settings: IndexerSettings) -> Vault:
    if settings.deid_database_url and (settings.vault_key or os.getenv("DEID_VAULT_KEY_FILE")):
        vault = PostgresVault(settings.deid_database_url, settings.vault_key_bytes())
        return vault
    log.warning("no DEID_DATABASE_URL: using an in-memory vault (offsets not persisted)")
    return MemoryVault()


def build_deps(settings: IndexerSettings | None = None) -> IndexerDeps:
    settings = settings or get_settings()
    store = build_store(settings)
    pool = ConnectionPool(settings.database_url, min_size=1, max_size=5, open=True)
    cursor = PostgresCursor(pool)
    cursor.migrate()
    clinical_repo = PostgresClinicalRepo(pool)
    clinical_repo.migrate()
    salt = settings.salt_bytes()
    vault = build_vault(settings)
    default_project = os.getenv("DEFAULT_PROJECT", "SCOUT_DEMO")
    pipeline = Pipeline(store, salt=salt, vault=vault, default_project=default_project)

    worker: Worker | None = None
    if settings.worker_enabled and settings.source != "none":
        source: XnatSource | FolderSource
        if settings.source == "xnat":
            client = XnatClient(settings.xnat_url, settings.xnat_user, settings.xnat_secret())
            source = XnatSource(client, cursor, max_attempts=settings.max_attempts)
        else:
            source = FolderSource(settings.folder_path, project=default_project)
        worker = Worker(
            pipeline,
            source,
            cursor=cursor,
            poll_seconds=settings.poll_seconds,
            max_attempts=settings.max_attempts,
            batch_size=settings.batch_size,
        )

    def postgres() -> bool:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return True

    return IndexerDeps(
        store=store,
        validator=JWTValidator(
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            jwks_url=settings.auth_jwks_url,
        ),
        audit=AuditLogger(store.client, service=SERVICE_NAME),
        worker=worker,
        cursor=cursor,
        clinical=ClinicalIngestor(
            store, clinical_repo, salt=salt, vault=vault, default_project=default_project
        ),
        webhook_token=settings.webhook_secret(),
        ready_checks=(postgres,),
    )


def check_database(url: str) -> bool:
    with psycopg.connect(url, connect_timeout=3) as conn:
        conn.execute("SELECT 1")
    return True
