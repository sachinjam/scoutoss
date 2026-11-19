"""Repo-wide pytest configuration: shared fixtures for every service's tests."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Tests never call a real LLM; tracing tests use in-memory exporters only.
os.environ.pop("OTEL_SDK_DISABLED", None)
os.environ.setdefault("LLM_PROVIDER", "fake")

from tests.support.fake_opensearch import FakeOpenSearch  # noqa: E402

ISSUER = "https://auth.test/realms/scoutoss"


@pytest.fixture
def salt() -> bytes:
    return b"test-site-salt-not-a-secret"


@pytest.fixture
def vault_key() -> bytes:
    return bytes(range(32))


@pytest.fixture
def fake_os() -> FakeOpenSearch:
    return FakeOpenSearch()


@pytest.fixture(scope="session")
def rsa_keys() -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    good = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    evil = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(good.public_key()))
    jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    return {"private": good, "evil": evil, "jwks": {"keys": [jwk]}}


@pytest.fixture(scope="session")
def make_token(rsa_keys: dict[str, Any]) -> Callable[..., str]:
    import jwt

    def _make(
        *,
        roles: tuple[str, ...] = ("researcher",),
        audience: str | list[str] = "scoutoss-copilot",
        issuer: str = ISSUER,
        expires_in: int = 300,
        key: Any = None,
        kid: str = "test-key",
        sub: str = "user-123",
        username: str = "rita.researcher",
    ) -> str:
        now = int(time.time())
        claims = {
            "sub": sub,
            "preferred_username": username,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + expires_in,
            "azp": "scoutoss-ui",
            "realm_access": {"roles": list(roles)},
        }
        return jwt.encode(
            claims, key or rsa_keys["private"], algorithm="RS256", headers={"kid": kid}
        )

    return _make


@pytest.fixture
def b64key(vault_key: bytes) -> str:
    return base64.b64encode(vault_key).decode()


@pytest.fixture
def tmp_cohort(tmp_path: Path) -> Iterator[Path]:
    from scoutoss_indexer.synthetic import default_cohort, write_cohort

    studies = default_cohort()
    for s in studies:
        for series in s.series:
            series.slices = 2
    write_cohort(tmp_path / "cohort", studies, size=16)
    return tmp_path / "cohort"
