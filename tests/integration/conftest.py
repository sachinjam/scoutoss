"""Integration fixtures: a real Postgres 15 and OpenSearch 2.19.

Uses ``TEST_PG_DSN`` / ``TEST_OS_URL`` when set (docker-compose.test.yml), otherwise
starts throwaway containers with testcontainers. Each test gets fresh indices/schemas.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest
from opensearchpy import OpenSearch

PG_IMAGE = "postgres:15.20-alpine"
OS_IMAGE = "opensearchproject/opensearch:2.19.3"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    if os.getenv("TEST_PG_DSN"):
        yield os.environ["TEST_PG_DSN"]
        return
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer(PG_IMAGE)
        .with_env("POSTGRES_PASSWORD", "postgres")
        .with_env("POSTGRES_DB", "scoutoss")
        .with_exposed_ports(5432)
    )
    with container:
        dsn = (
            f"postgresql://postgres:postgres@{container.get_container_host_ip()}:"
            f"{container.get_exposed_port(5432)}/scoutoss"
        )
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                with psycopg.connect(dsn, connect_timeout=3) as conn:
                    conn.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                time.sleep(1)
        else:  # pragma: no cover
            raise RuntimeError("Postgres did not start")
        yield dsn


@pytest.fixture(scope="session")
def os_url() -> Iterator[str]:
    if os.getenv("TEST_OS_URL"):
        yield os.environ["TEST_OS_URL"]
        return
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer(OS_IMAGE)
        .with_env("discovery.type", "single-node")
        .with_env("DISABLE_SECURITY_PLUGIN", "true")
        .with_env("DISABLE_INSTALL_DEMO_CONFIG", "true")
        .with_env("OPENSEARCH_JAVA_OPTS", "-Xms512m -Xmx512m")
        .with_exposed_ports(9200)
    )
    with container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9200)}"
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                if httpx.get(f"{url}/_cluster/health", timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:  # pragma: no cover
            raise RuntimeError("OpenSearch did not start")
        yield url


@pytest.fixture
def os_client(os_url: str) -> Iterator[OpenSearch]:
    client = OpenSearch(hosts=[os_url], timeout=30)
    client.indices.delete(
        index="dicom_*,indexer_dlq,clinical_events,audit",
        ignore_unavailable=True,
        expand_wildcards="all",
    )
    yield client
    client.close()


@pytest.fixture
def pg(pg_dsn: str) -> Iterator[str]:
    """A fresh database per test (schemas indexer/clinical/deid/copilot are created by the code)."""
    name = f"t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    base = pg_dsn.rsplit("/", 1)[0]
    yield f"{base}/{name}"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def count(client: OpenSearch, index: str, query: dict[str, Any] | None = None) -> int:
    client.indices.refresh(index=index)
    return int(client.count(index=index, body={"query": query or {"match_all": {}}})["count"])
