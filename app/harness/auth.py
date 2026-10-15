"""JWT authentication. Tokens carry sub (email), tenant_id and role; membership is checked by tenant-mcp."""

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, cast

import jwt

from app.core.config import get_settings
from app.harness.context import ROLE_RANK, Caller, Identity, Role


class AuthError(Exception):
    pass


@lru_cache
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url)


def create_token(tenant_id: str, email: str, role: str, ttl_minutes: int | None = None) -> str:
    """Issue an HS256 token. For development and tests; production tokens come from the IdP."""
    settings = get_settings()
    if settings.app_env == "prod":
        raise AuthError("Local token issuing is disabled in production")
    now = datetime.now(UTC)
    claims = {
        "sub": email,
        "tenant_id": tenant_id,
        "role": role,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes or settings.jwt_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    options = {"require": ["exp", "sub", "iss", "aud"]}
    try:
        if settings.jwt_jwks_url:
            key = _jwks_client(settings.jwt_jwks_url).get_signing_key_from_jwt(token).key
            algorithms = ["RS256"]
        else:
            key, algorithms = settings.jwt_secret, ["HS256"]
        return jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options=options,  # type: ignore[arg-type]
        )
    except jwt.PyJWTError as e:
        raise AuthError(f"Invalid token: {e}") from e


def authenticate(token: str, request_id: str = "") -> Caller:
    """Verify the token locally, then ask tenant-mcp about membership.

    The effective role is the lower of the token role and the database role.
    """
    from app.harness import tenant_client  # deferred: tenant_client pulls in the MCP client stack

    claims = decode_token(token)
    tenant_id, email, token_role = claims.get("tenant_id"), claims["sub"], claims.get("role", "viewer")
    if not isinstance(tenant_id, str) or token_role not in ROLE_RANK:
        raise AuthError("Token is missing tenant_id or has an unknown role")
    info = tenant_client.whoami(tenant_id, email, token_role, request_id)
    if not info["member"]:
        raise AuthError("User is not a member of this tenant")
    identity = Identity(tenant_id=tenant_id, user_email=email, role=cast(Role, info["role"]))
    return Caller(identity=identity, tenant_active=info["tenant_active"], page_names=tuple(info["page_names"]))
