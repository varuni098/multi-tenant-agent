"""Signed context the agent app attaches to every tenant-mcp call (header X-Tenant-Context).

It names who is acting (tenant, user, role) and why (purpose). The server verifies the
signature, then re-checks membership and role in the database.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt

from app.core.config import get_settings

Purpose = Literal["auth", "agent", "confirm"]
HEADER = "X-Tenant-Context"
_ISSUER, _AUDIENCE = "agent-app", "tenant-mcp"


@dataclass(frozen=True)
class ServiceContext:
    tenant_id: str
    user_email: str
    role: str
    purpose: Purpose
    request_id: str


class InvalidContext(Exception):
    pass


def sign_context(ctx: ServiceContext) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    claims = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "sub": ctx.user_email,
        "tenant_id": ctx.tenant_id,
        "role": ctx.role,
        "purpose": ctx.purpose,
        "rid": ctx.request_id,
        "iat": now,
        "exp": now + timedelta(seconds=s.mcp_context_ttl_seconds),
    }
    return jwt.encode(claims, s.mcp_context_secret, algorithm="HS256")


def verify_context(token: str) -> ServiceContext:
    try:
        claims = jwt.decode(
            token,
            get_settings().mcp_context_secret,
            algorithms=["HS256"],
            audience=_AUDIENCE,
            issuer=_ISSUER,
            options={"require": ["exp", "sub", "tenant_id", "role", "purpose"]},
        )
    except jwt.PyJWTError as e:
        raise InvalidContext(f"Invalid service context: {e}") from e
    if claims["purpose"] not in ("auth", "agent", "confirm"):
        raise InvalidContext("Invalid service context purpose")
    return ServiceContext(
        tenant_id=claims["tenant_id"],
        user_email=claims["sub"],
        role=claims["role"],
        purpose=claims["purpose"],
        request_id=str(claims.get("rid", ""))[:64],
    )
