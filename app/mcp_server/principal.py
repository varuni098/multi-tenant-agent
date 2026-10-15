"""Who is calling tenant-mcp, resolved once per HTTP request by AuthMiddleware.

Two ways in:
- X-Tenant-Context: signed by the agent app (purpose auth / agent / confirm)
- Authorization: Bearer <user JWT>: external MCP clients (Claude Desktop, IDEs); purpose "external"

Except for purpose "auth" (whoami answers membership itself), the user must belong to the
tenant, the tenant must be active, and the role is capped by the database role.
"""

import uuid
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

# Which credential purposes each path accepts; everything else is the MCP endpoint
PATH_PURPOSES = {
    "/internal/whoami": {"auth"},
    "/internal/confirm": {"confirm"},
}
MCP_PURPOSES = {"agent", "external"}
PUBLIC_PATHS = {"/health"}


@dataclass(frozen=True)
class Principal:
    identity: Identity
    purpose: str
    request_id: str


class AuthFailed(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def resolve_principal(context_header: str | None, authorization: str | None, path: str) -> Principal:
    tenant_id: str
    email: str
    claimed_role: str
    purpose: str
    request_id: str
    if context_header:
        try:
            ctx = verify_context(context_header)
        except InvalidContext as e:
            raise AuthFailed(401, str(e)) from e
        tenant_id, email, claimed_role, purpose, request_id = (
            ctx.tenant_id,
            ctx.user_email,
            ctx.role,
            ctx.purpose,
            ctx.request_id,
        )
    elif authorization and authorization.lower().startswith("bearer "):
        try:
            claims = decode_token(authorization[7:].strip())
        except AuthError as e:
            raise AuthFailed(401, str(e)) from e
        claimed_tenant = claims.get("tenant_id")
        if not isinstance(claimed_tenant, str):
            raise AuthFailed(401, "Token is missing tenant_id")
        tenant_id, email = claimed_tenant, claims["sub"]
        claimed_role, purpose, request_id = claims.get("role", "viewer"), "external", uuid.uuid4().hex
    else:
        raise AuthFailed(401, "Missing credentials")

    allowed = next((p for prefix, p in PATH_PURPOSES.items() if path.startswith(prefix)), MCP_PURPOSES)
    if purpose not in allowed:
        raise AuthFailed(403, "These credentials are not valid for this endpoint")
    if claimed_role not in ROLE_RANK:
        raise AuthFailed(401, "Unknown role")

    if purpose != "auth":
        db_role = get_user_role(tenant_id, email)
        if db_role is None:
            raise AuthFailed(403, "User is not a member of this tenant")
        if not is_tenant_active(tenant_id):
            raise AuthFailed(403, "Tenant is inactive")
        claimed_role = min(claimed_role, db_role, key=ROLE_RANK.__getitem__)

    identity = Identity(tenant_id=tenant_id, user_email=email, role=cast(Role, claimed_role))
    return Principal(identity=identity, purpose=purpose, request_id=request_id)


class AuthMiddleware:
    """Rejects unauthenticated requests with 401/403 before MCP sees them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        try:
            principal = await anyio.to_thread.run_sync(
                resolve_principal, headers.get(HEADER), headers.get("authorization"), scope["path"]
            )
        except AuthFailed as e:
            extra = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
            await JSONResponse({"error": str(e)}, status_code=e.status, headers=extra)(scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)
