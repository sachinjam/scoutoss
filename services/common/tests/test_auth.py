from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scoutoss_common.auth import (
    AuthError,
    JWTValidator,
    Principal,
    make_auth_dependency,
    require_roles,
    roles_from_claims,
)

ISSUER = "https://auth.test/realms/scoutoss"


@pytest.fixture
def validator(rsa_keys: dict[str, Any]) -> JWTValidator:
    return JWTValidator(issuer=ISSUER, audience="scoutoss-copilot", jwks=rsa_keys["jwks"])


def test_valid_token(validator: JWTValidator, make_token: Callable[..., str]) -> None:
    p = validator.principal(make_token(roles=("researcher",)))
    assert p.username == "rita.researcher"
    assert "researcher" in p.roles
    assert p.pseudonym.startswith("u_") and "user-123" not in p.pseudonym


def test_audience_list_accepted(validator: JWTValidator, make_token: Callable[..., str]) -> None:
    validator.validate(make_token(audience=["account", "scoutoss-copilot"]))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"expires_in": -3600}, "token expired"),
        ({"audience": "someone-else"}, "wrong audience"),
        ({"issuer": "https://evil.test/realms/x"}, "wrong issuer"),
    ],
)
def test_rejections(
    validator: JWTValidator, make_token: Callable[..., str], kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(AuthError, match=message):
        validator.validate(make_token(**kwargs))


def test_wrong_key(
    validator: JWTValidator, make_token: Callable[..., str], rsa_keys: dict[str, Any]
) -> None:
    with pytest.raises(AuthError, match="bad signature"):
        validator.validate(make_token(key=rsa_keys["evil"]))


def test_unknown_kid(validator: JWTValidator, make_token: Callable[..., str]) -> None:
    with pytest.raises(AuthError, match="unknown signing key"):
        validator.validate(make_token(kid="other"))


def test_malformed_and_hs256(validator: JWTValidator) -> None:
    import jwt

    with pytest.raises(AuthError, match="malformed"):
        validator.validate("not-a-jwt")
    hs = jwt.encode({"sub": "x"}, "secret-secret-secret-secret-secret!", algorithm="HS256")
    with pytest.raises(AuthError, match="unsupported algorithm"):
        validator.validate(hs)


def test_validator_requires_keys() -> None:
    with pytest.raises(ValueError, match="jwks"):
        JWTValidator(issuer=ISSUER, audience="a")


def test_jwks_url_client(
    rsa_keys: dict[str, Any], make_token: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jwt import PyJWKClient

    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: rsa_keys["jwks"])
    v = JWTValidator(issuer=ISSUER, audience="scoutoss-copilot", jwks_url="http://kc/certs")
    assert v.validate(make_token())["sub"] == "user-123"
    with pytest.raises(AuthError):
        v.validate(make_token(kid="nope"))


def test_projects_from_claims_and_scope(
    validator: JWTValidator, make_token: Callable[..., str]
) -> None:
    from scoutoss_common.auth import projects_from_claims

    assert projects_from_claims(
        {"groups": ["/projects/A", "/projects/B/sub", "/other"], "projects": ["C"]}
    ) == {"A", "B", "C"}
    p = validator.principal(make_token(groups=("/projects/SCOUT_DEMO",)))
    assert p.projects == {"SCOUT_DEMO"} and p.project_scope() == {"SCOUT_DEMO"}
    admin = validator.principal(make_token(roles=("admin",), groups=()))
    assert admin.all_projects and admin.project_scope() is None


def test_roles_from_claims() -> None:
    claims = {
        "realm_access": {"roles": ["viewer"]},
        "resource_access": {"svc": {"roles": ["admin"]}},
    }
    assert roles_from_claims(claims, "svc") == {"viewer", "admin"}
    assert roles_from_claims({}, None) == set()


def test_fastapi_dependencies(validator: JWTValidator, make_token: Callable[..., str]) -> None:
    app = FastAPI()
    auth = make_auth_dependency(lambda: validator)
    admin = require_roles(auth, "admin")

    @app.get("/me")
    def me(p: Principal = Depends(auth)) -> dict[str, str]:
        return {"user": p.pseudonym}

    @app.get("/admin")
    def adm(p: Principal = Depends(admin)) -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers={"Authorization": "Basic abc"}).status_code == 401
    bad = client.get("/me", headers={"Authorization": f"Bearer {make_token(expires_in=-999)}"})
    assert bad.status_code == 401 and bad.json()["detail"] == "token expired"
    ok = client.get("/me", headers={"Authorization": f"Bearer {make_token()}"})
    assert ok.status_code == 200
    assert (
        client.get("/admin", headers={"Authorization": f"Bearer {make_token()}"}).status_code == 403
    )
    tok = make_token(roles=("admin",))
    assert client.get("/admin", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
