"""Who is calling an MCP server, resolved once per HTTP request by AuthMiddleware.

Two ways in:
- X-Tenant-Context: signed by the agent app (purposes: auth, agent, confirm)
- Authorization: Bearer <user JWT>: external MCP clients (Claude Desktop, IDEs); purpose "external"

Credential parsing and purpose binding are shared. Each server supplies its own membership
check: tenant-mcp asks MySQL (the system of record); graph-mcp asks the graph.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import anyio
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.service_context import HEADER, InvalidContext, verify_context
from app.harness.auth import AuthError, decode_token
from app.harness.context import ROLE_RANK, Identity, Role
from app.services.tenants import get_user_role, is_tenant_active

MCP_PURPOSES = {"agent", "external"}
PUBLIC_PATHS = {"/health"}

# tenant-mcp: which credential purposes each path accepts; everything else is the MCP endpoint
TENANT_PATH_PURPOSES = {
    "/internal/whoami": {"auth"},
    "/internal/confirm": {"confirm"},
    "/internal/settings": {"api"},  # REST API reads for the UI
    "/internal/audit": {"api"},
}


@dataclass(frozen=True)
class Principal:
    identity: Identity
    purpose: str
    request_id: str


@dataclass(frozen=True)
class Credentials:
    tenant_id: str
    email: str
    role: str
    purpose: str
    request_id: str


class AuthFailed(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


Resolver = Callable[[str | None, str | None, str], Principal]


def parse_credentials(context_header: str | None, authorization: str | None) -> Credentials:
    if context_header:
        try:
            ctx = verify_context(context_header)
        except InvalidContext as e:
            raise AuthFailed(401, str(e)) from e
        creds = Credentials(ctx.tenant_id, ctx.user_email, ctx.role, ctx.purpose, ctx.request_id)
    elif authorization and authorization.lower().startswith("bearer "):
        try:
            claims = decode_token(authorization[7:].strip())
        except AuthError as e:
            raise AuthFailed(401, str(e)) from e
        tenant_id = claims.get("tenant_id")
        if not isinstance(tenant_id, str):
            raise AuthFailed(401, "Token is missing tenant_id")
        creds = Credentials(tenant_id, claims["sub"], claims.get("role", "viewer"), "external", uuid.uuid4().hex)
    else:
        raise AuthFailed(401, "Missing credentials")
    if creds.role not in ROLE_RANK:
        raise AuthFailed(401, "Unknown role")
    return creds


def check_purpose(purpose: str, path: str, path_purposes: dict[str, set[str]]) -> None:
    allowed = next((p for prefix, p in path_purposes.items() if path.startswith(prefix)), MCP_PURPOSES)
    if purpose not in allowed:
        raise AuthFailed(403, "These credentials are not valid for this endpoint")


def capped(claimed: str, actual: str) -> Role:
    return cast(Role, min(claimed, actual, key=ROLE_RANK.__getitem__))


def resolve_principal(context_header: str | None, authorization: str | None, path: str) -> Principal:
    """tenant-mcp: membership, tenant status and role checked in MySQL (except purpose auth)."""
    c = parse_credentials(context_header, authorization)
    check_purpose(c.purpose, path, TENANT_PATH_PURPOSES)
    role = c.role
    if c.purpose != "auth":  # whoami answers membership itself
        db_role = get_user_role(c.tenant_id, c.email)
        if db_role is None:
            raise AuthFailed(403, "User is not a member of this tenant")
        if not is_tenant_active(c.tenant_id):
            raise AuthFailed(403, "Tenant is inactive")
        role = capped(c.role, db_role)
    identity = Identity(tenant_id=c.tenant_id, user_email=c.email, role=cast(Role, role))
    return Principal(identity=identity, purpose=c.purpose, request_id=c.request_id)


class AuthMiddleware:
    """Rejects unauthenticated requests with 401/403 before MCP sees them."""

    def __init__(self, app: ASGIApp, resolver: Resolver = resolve_principal) -> None:
        self.app = app
        self.resolver = resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        try:
            principal = await anyio.to_thread.run_sync(
                self.resolver, headers.get(HEADER), headers.get("authorization"), scope["path"]
            )
        except AuthFailed as e:
            extra = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
            await JSONResponse({"error": str(e)}, status_code=e.status, headers=extra)(scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)
