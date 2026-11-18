from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import jwt

from .errors import ApiError

REQUIRED_ROLE = "RERO_CALLER"


class KeyResolver(Protocol):
    def signing_key(self, token: str) -> Any: ...


class JwksKeyResolver:
    """Resolves the signing key from the issuer JWKS (PyJWT client, cached keys)."""

    def __init__(self, jwks_uri: str) -> None:
        self._client = jwt.PyJWKClient(jwks_uri, cache_keys=True, timeout=5)

    def signing_key(self, token: str) -> Any:
        return self._client.get_signing_key_from_jwt(token).key


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject: str
    # The caller's own service token: forwarded to the gateway, never logged or stored.
    token: str = field(repr=False)


def _roles(claims: dict[str, Any]) -> set[str]:
    roles: set[str] = set()
    realm = claims.get("realm_access")
    if isinstance(realm, dict) and isinstance(realm.get("roles"), list):
        roles.update(r for r in realm["roles"] if isinstance(r, str))
    if isinstance(claims.get("roles"), list):
        roles.update(r for r in claims["roles"] if isinstance(r, str))
    return roles


def _audiences(claims: dict[str, Any]) -> set[str]:
    aud = claims.get("aud")
    if isinstance(aud, str):
        return {aud}
    if isinstance(aud, list):
        return {a for a in aud if isinstance(a, str)}
    return set()


def authenticate(
    token: str, *, issuer: str, required_audiences: list[str], resolver: KeyResolver
) -> Principal:
    unauthenticated = ApiError(401, "UNAUTHENTICATED", "Missing or invalid service token.")
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":
            raise unauthenticated
        key = resolver.signing_key(token)
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False, "require": ["exp", "iss"]},
        )
    except ApiError:
        raise
    except jwt.PyJWKClientConnectionError as exc:
        raise ApiError(503, "JWKS_UNAVAILABLE", "Signing keys are temporarily unavailable.") from exc
    except Exception as exc:  # signature, expiry, issuer, malformed, unknown kid
        raise unauthenticated from exc

    if not set(required_audiences) <= _audiences(claims) or REQUIRED_ROLE not in _roles(claims):
        raise ApiError(403, "WRONG_AUDIENCE_OR_ROLE", "Token audience or role is not accepted.")
    tenant = claims.get("tenant_id")
    if not isinstance(tenant, str) or not tenant.strip():
        raise unauthenticated
    subject = claims.get("sub")
    return Principal(tenant_id=tenant, subject=subject if isinstance(subject, str) else "", token=token)
