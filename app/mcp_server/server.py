"""tenant-mcp: page/settings tools over MCP (streamable HTTP), plus internal routes for the agent app.

    uvicorn app.mcp_server.server:app --port 8001

No tool takes a tenant argument; the tenant comes from the caller's credentials
(see principal.py). Every call is policy-checked and logged to tool_calls.
"""

import functools
import logging
import time
from collections.abc import Callable
from typing import Any

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from mcp.types import ToolAnnotations
from pydantic import BaseModel
from sqlalchemy import select
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.logging import setup_logging
from app.db.models import ToolCall
from app.db.session import ping, session_scope
from app.harness.context import ROLE_RANK
from app.harness.guardrails import redact
from app.harness.policy import TOOL_POLICIES, role_allows
from app.mcp_server import confirmations
from app.mcp_server.principal import AuthMiddleware, Principal
from app.services import admin
from app.services import settings as svc
from app.services.errors import DomainError
from app.services.schemas import AuditEntry, ChangeResult, PageInfo, SettingValue
from app.services.tenants import get_user_role, is_tenant_active

setup_logging()
log = logging.getLogger(__name__)

mcp = FastMCP(
    "tenant-tools",
    instructions=(
        "Read and change one tenant's page settings (theme, language, layout, brand_color, show_tips). "
        "The tenant and your permissions come from your credentials. "
        "Destructive or bulk actions return a confirmation token; the user approves them in the app."
    ),
    mask_error_details=True,
)

IDEMPOTENT = {"set_theme", "set_setting"}


class CreatedPage(BaseModel):
    name: str
    theme: str
    changed: bool = True
    message: str


class ConfirmationRequired(BaseModel):
    confirmation_required: bool = True
    token: str
    summary: str
    expires_in: int


def _principal() -> Principal:
    return get_http_request().state.principal


def _log_call(p: Principal, tool: str, args: dict[str, Any], outcome: str, started: float) -> None:
    duration = int((time.perf_counter() - started) * 1000)
    log.info("[%s] %s %s(%s) -> %s", p.request_id, p.purpose, tool, redact(repr(args)), outcome)
    try:
        with session_scope() as session:
            session.add(
                ToolCall(
                    tenant_id=p.identity.tenant_id,
                    actor=p.identity.user_email,
                    channel=p.purpose,
                    tool=tool,
                    args={k: redact(v) if isinstance(v, str) else v for k, v in args.items()},
                    outcome=outcome,
                    duration_ms=duration,
                    request_id=p.request_id or None,
                )
            )
    except Exception:  # logging must never break the call
        log.exception("Failed to record tool call")


def tool(fn: Callable[..., Any]) -> Any:
    """Register a tool with annotations from the shared policy, a role check, error mapping and call logging."""
    name = fn.__name__
    policy = TOOL_POLICIES[name]

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> Any:
        p = _principal()
        started = time.perf_counter()
        if not role_allows(p.identity.role, name):
            _log_call(p, name, kwargs, "denied", started)
            raise ToolError(f"You are not permitted to use {name}.")
        try:
            result = fn(**kwargs)
        except DomainError as e:
            _log_call(p, name, kwargs, "error", started)
            raise ToolError(str(e)) from e
        _log_call(p, name, kwargs, "ok", started)
        return result

    annotations = ToolAnnotations(
        read_only_hint=not policy.writes,
        destructive_hint=policy.needs_confirmation,
        idempotent_hint=not policy.writes or name in IDEMPOTENT,
        open_world_hint=False,
    )
    return mcp.tool(name=name, annotations=annotations, run_in_thread=True)(wrapper)


def _tenant() -> str:
    return _principal().identity.tenant_id


def _actor() -> dict[str, Any]:
    p = _principal()
    return {"actor": p.identity.user_email, "request_id": p.request_id or None}


# --- Read tools ---


@tool
def list_pages() -> list[PageInfo]:
    """List every page with its current theme."""
    return svc.list_pages(_tenant())


@tool
def search_pages(query: str) -> list[PageInfo]:
    """Find pages whose name matches loosely, e.g. 'login screen' finds 'login'.

    Args:
        query: Words describing the page.
    """
    return svc.search_pages(_tenant(), query)


@tool
def get_theme(page_name: str) -> SettingValue:
    """Get a page's current theme (light/dark) and where it comes from.

    Args:
        page_name: The page's short identifier, e.g. 'login' or 'dashboard' (never include the word 'page').
    """
    return svc.get_setting(_tenant(), "theme", page_name)


@tool
def get_settings_for_page(page_name: str | None = None) -> list[SettingValue]:
    """Get every effective setting of a page (or tenant-wide defaults when page_name is omitted).

    Args:
        page_name: The page's short identifier, or omit for tenant-wide defaults.
    """
    return svc.get_effective_settings(_tenant(), page_name)


@tool
def get_change_history(page_name: str | None = None) -> list[AuditEntry]:
    """Recent changes (who changed what, when), newest first. A null before/after means "inherited".

    Args:
        page_name: Limit to one page, or omit for all pages.
    """
    return svc.get_change_history(_tenant(), page_name)


# --- Write tools ---


@tool
def set_theme(page_name: str, theme: str) -> ChangeResult:
    """Set a page's theme to an exact value. Safe to repeat: setting the current value changes nothing.

    Args:
        page_name: The page's short identifier, e.g. 'login' or 'dashboard' (never include the word 'page').
        theme: 'light' or 'dark'.
    """
    return svc.set_theme(_tenant(), page_name, theme, **_actor())


@tool
def set_setting(key: str, value: str, page_name: str | None = None) -> ChangeResult:
    """Set any setting (language, layout, brand_color, show_tips, theme) on a page or tenant-wide.

    Args:
        key: Setting name, e.g. 'language' or 'layout'.
        value: New value, e.g. 'fr', 'compact', '#ff0000', 'true'.
        page_name: The page's short identifier, or omit to set the tenant-wide default.
    """
    return svc.set_setting(_tenant(), key, value, page_name, **_actor())


@tool
def undo_last_change(page_name: str | None = None) -> ChangeResult:
    """Revert the most recent setting change (optionally only for one page).

    Args:
        page_name: Limit to one page, or omit to undo the latest change anywhere.
    """
    return svc.undo_last_change(_tenant(), page_name, **_actor())


# --- Admin tools ---


@tool
def create_page(page_name: str, theme: str | None = None) -> CreatedPage:
    """Create a new page.

    Args:
        page_name: Short identifier for the new page, e.g. 'billing'.
        theme: Optional starting theme, 'light' or 'dark'.
    """
    page = admin.create_page(_tenant(), page_name, theme, **_actor())
    return CreatedPage(name=page.name, theme=page.theme, message=f"Created page '{page.name}'.")


@tool
def delete_page(page_name: str) -> ConfirmationRequired:
    """Delete a page and its overrides. Requires the user's confirmation; returns a confirmation token.

    Args:
        page_name: The page's short identifier.
    """
    name, summary = admin.preview_delete_page(_tenant(), page_name)
    return ConfirmationRequired(
        **confirmations.request_confirmation(_principal().identity, "delete_page", {"page_name": name}, summary)
    )


@tool
def bulk_set_setting(page_names: list[str], key: str, value: str) -> ConfirmationRequired:
    """Set one setting on several pages at once. Requires the user's confirmation; returns a confirmation token.

    Args:
        page_names: Short identifiers of the pages.
        key: Setting name, e.g. 'theme'.
        value: New value.
    """
    names, normalised, summary = admin.preview_bulk_set_setting(_tenant(), page_names, key, value)
    args = {"page_names": names, "key": key, "value": normalised}
    return ConfirmationRequired(
        **confirmations.request_confirmation(_principal().identity, "bulk_set_setting", args, summary)
    )


@tool
def apply_template(template_name: str) -> ConfirmationRequired:
    """Switch the tenant's base template ('standard' or 'dark_first'). Requires confirmation; returns a token.

    Args:
        template_name: Template name.
    """
    name, summary = admin.preview_apply_template(_tenant(), template_name)
    return ConfirmationRequired(
        **confirmations.request_confirmation(_principal().identity, "apply_template", {"template_name": name}, summary)
    )


# --- Internal routes for the agent app (not MCP tools, so external clients never see them) ---


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    try:
        await anyio.to_thread.run_sync(ping)
        return JSONResponse({"status": "ok", "database": True})
    except Exception:
        return JSONResponse({"status": "degraded", "database": False}, status_code=503)


def _whoami(p: Principal) -> dict[str, Any]:
    tenant_id, email = p.identity.tenant_id, p.identity.user_email
    db_role = get_user_role(tenant_id, email)
    if db_role is None:
        return {"member": False, "role": None, "tenant_active": False, "page_names": []}
    active = is_tenant_active(tenant_id)
    return {
        "member": True,
        "role": min(p.identity.role, db_role, key=ROLE_RANK.__getitem__),
        "tenant_active": active,
        "page_names": [page.name for page in svc.list_pages(tenant_id)] if active else [],
    }


@mcp.custom_route("/internal/whoami", methods=["POST"])
async def whoami(request: Request) -> JSONResponse:
    """Membership, effective role, tenant status and page names (purpose: auth)."""
    return JSONResponse(await anyio.to_thread.run_sync(_whoami, request.state.principal))


@mcp.custom_route("/internal/confirm", methods=["POST"])
async def confirm(request: Request) -> JSONResponse:
    """Execute a pending confirmation (purpose: confirm)."""
    p: Principal = request.state.principal
    token = (await request.json()).get("token", "")
    try:
        message = await anyio.to_thread.run_sync(confirmations.confirm, p.identity, str(token), p.request_id)
    except (confirmations.ConfirmationError, DomainError) as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return JSONResponse({"message": message})


def _settings_view(p: Principal) -> dict[str, Any]:
    tenant_id = p.identity.tenant_id
    pages = svc.list_pages(tenant_id)
    return {
        "defaults": [s.model_dump() for s in svc.get_effective_settings(tenant_id)],
        "pages": [
            {"name": page.name, "settings": [s.model_dump() for s in svc.get_effective_settings(tenant_id, page.name)]}
            for page in pages
        ],
    }


@mcp.custom_route("/internal/settings", methods=["POST"])
async def settings_view(request: Request) -> JSONResponse:
    """Effective settings of every page plus tenant defaults, for the settings browser (purpose: api)."""
    return JSONResponse(await anyio.to_thread.run_sync(_settings_view, request.state.principal))


AUDIT_MAX = 200


def _tool_calls(tenant_id: str, limit: int, before_id: int | None, actor: str | None) -> list[dict[str, Any]]:
    with session_scope() as session:
        stmt = select(ToolCall).where(ToolCall.tenant_id == tenant_id)
        if before_id:
            stmt = stmt.where(ToolCall.id < before_id)
        if actor:
            stmt = stmt.where(ToolCall.actor == actor)
        rows = session.scalars(stmt.order_by(ToolCall.id.desc()).limit(limit))
        return [
            {
                "id": r.id,
                "actor": r.actor,
                "channel": r.channel,
                "tool": r.tool,
                "args": r.args,
                "outcome": r.outcome,
                "duration_ms": r.duration_ms,
                "request_id": r.request_id,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


def _audit(p: Principal, body: dict[str, Any]) -> list[dict[str, Any]]:
    limit = max(1, min(int(body.get("limit") or 50), AUDIT_MAX))
    before_id = int(body["before_id"]) if body.get("before_id") else None
    actor = body.get("actor") or None
    if body.get("kind") == "tool_calls":
        return _tool_calls(p.identity.tenant_id, limit, before_id, actor)
    entries = svc.get_change_history(
        p.identity.tenant_id, body.get("page_name") or None, limit, before_id=before_id, actor=actor
    )
    return [e.model_dump(mode="json") for e in entries]


@mcp.custom_route("/internal/audit", methods=["POST"])
async def audit(request: Request) -> JSONResponse:
    """Setting changes or tool calls for the caller's tenant, newest first (purpose: api, admin only)."""
    p: Principal = request.state.principal
    if p.identity.role != "admin":
        return JSONResponse({"error": "Only admins can read the audit log."}, status_code=403)
    try:
        body = await request.json()
        rows = await anyio.to_thread.run_sync(_audit, p, body if isinstance(body, dict) else {})
    except (ValueError, TypeError):
        return JSONResponse({"error": "Invalid audit query."}, status_code=400)
    return JSONResponse({"items": rows})


app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True, middleware=[Middleware(AuthMiddleware)])
