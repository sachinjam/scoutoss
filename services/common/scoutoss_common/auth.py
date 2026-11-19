"""Keycloak (OIDC) JWT validation for FastAPI.

Tokens must be RS256, unexpired, issued by ``AUTH_ISSUER`` and carry ``AUTH_AUDIENCE``
in ``aud``. Keys come from the realm JWKS endpoint (cached) — or, in tests, from an
in-memory JWKS. Roles are read from ``realm_access.roles`` and the service client's
``resource_access`` entry.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import jwt
import structlog
from fastapi import Depends, HTTPException, Request, status
from jwt import PyJWK, PyJWKClient, PyJWKSet

ALGORITHMS = ["RS256"]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. ``pseudonym`` is what goes into logs and audit."""

    subject: str
    username: str
    roles: frozenset[str] = field(default_factory=frozenset)
    client_id: str | None = None
    token: str = field(default="", repr=False)

    @property
    def pseudonym(self) -> str:
        return "u_" + hashlib.sha256(self.subject.encode()).hexdigest()[:16]

    def has_any(self, roles: Iterable[str]) -> bool:
        return bool(self.roles.intersection(roles))


class AuthError(Exception):
    """Raised for any token problem; message is safe to return to the client."""


class JWTValidator:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        jwks: dict[str, Any] | None = None,
        leeway: int = 30,
        client_id: str | None = None,
    ) -> None:
        if not jwks_url and not jwks:
            raise ValueError("JWTValidator needs jwks_url or jwks")
        self.issuer = issuer
        self.audience = audience
        self.leeway = leeway
        self.client_id = client_id or audience
        self._client = PyJWKClient(jwks_url, cache_keys=True, lifespan=300) if jwks_url else None
        self._keyset = PyJWKSet.from_dict(jwks) if jwks else None

    def _signing_key(self, token: str) -> PyJWK:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError("malformed token") from exc
        if header.get("alg") not in ALGORITHMS:
            raise AuthError("unsupported algorithm")
        kid = header.get("kid")
        if self._keyset is not None:
            for key in self._keyset.keys:
                if kid is None or key.key_id == kid:
                    return key
            raise AuthError("unknown signing key")
        assert self._client is not None
        try:
            return self._client.get_signing_key_from_jwt(token)
        except jwt.PyJWKClientError as exc:
            raise AuthError("unknown signing key") from exc

    def validate(self, token: str) -> dict[str, Any]:
        key = self._signing_key(token)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("token expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError("wrong audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("wrong issuer") from exc
        except jwt.InvalidSignatureError as exc:
            raise AuthError("bad signature") from exc
        except jwt.PyJWTError as exc:
            raise AuthError("invalid token") from exc
        return claims

    def principal(self, token: str) -> Principal:
        claims = self.validate(token)
        return Principal(
            subject=str(claims["sub"]),
            username=str(claims.get("preferred_username", claims["sub"])),
            roles=frozenset(roles_from_claims(claims, self.client_id)),
            client_id=claims.get("azp"),
            token=token,
        )


def roles_from_claims(claims: dict[str, Any], client_id: str | None = None) -> set[str]:
    roles: set[str] = set(claims.get("realm_access", {}).get("roles", []))
    resource = claims.get("resource_access", {})
    if client_id and client_id in resource:
        roles.update(resource[client_id].get("roles", []))
    return roles


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


def make_auth_dependency(
    get_validator: Callable[[], JWTValidator],
) -> Callable[..., Principal]:
    """Build a FastAPI dependency that authenticates the request."""

    def authenticate(request: Request) -> Principal:
        token = bearer_token(request)
        try:
            principal = get_validator().principal(token)
        except AuthError as exc:
            structlog.contextvars.bind_contextvars(auth_error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            ) from exc
        structlog.contextvars.bind_contextvars(user_id=principal.pseudonym)
        request.state.principal = principal
        return principal

    return authenticate


def require_roles(authenticate: Callable[..., Principal], *roles: str) -> Callable[..., Principal]:
    """Dependency that additionally requires one of ``roles``."""

    def checker(principal: Principal = Depends(authenticate)) -> Principal:
        if roles and not principal.has_any(roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        return principal

    return checker


def jwks_from_json(text: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(text)
    return data
