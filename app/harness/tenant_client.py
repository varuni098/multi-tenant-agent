"""The agent app's only route to tenant data: tenant-mcp, over MCP and two internal routes."""

import logging
from typing import Any

import httpx
from pydantic_ai import RunContext
from pydantic_ai.mcp import CallToolFunc, MCPToolset
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset

from app.core.config import get_settings
from app.core.service_context import HEADER, Purpose, ServiceContext, sign_context
from app.harness.context import Action, Identity, PendingConfirmation, RunDeps
from app.harness.guardrails import redact

log = logging.getLogger(__name__)


class TenantServiceUnavailable(Exception):
    pass


class ConfirmationError(Exception):
    pass


def _headers(tenant_id: str, email: str, role: str, purpose: Purpose, request_id: str) -> dict[str, str]:
    ctx = ServiceContext(tenant_id=tenant_id, user_email=email, role=role, purpose=purpose, request_id=request_id)
    return {HEADER: sign_context(ctx)}


def _post(path: str, headers: dict[str, str], body: dict[str, Any] | None = None) -> httpx.Response:
    try:
        return httpx.post(f"{get_settings().tenant_mcp_base_url}{path}", headers=headers, json=body or {}, timeout=15)
    except httpx.HTTPError as e:
        raise TenantServiceUnavailable("The tenant service is unavailable. Please try again shortly.") from e


def whoami(tenant_id: str, email: str, role: str, request_id: str) -> dict[str, Any]:
    resp = _post("/internal/whoami", _headers(tenant_id, email, role, "auth", request_id))
    if resp.status_code != 200:
        raise TenantServiceUnavailable(f"Tenant service rejected the login check ({resp.status_code}).")
    return resp.json()


def confirm(identity: Identity, token: str, request_id: str) -> str:
    resp = _post(
        "/internal/confirm",
        _headers(identity.tenant_id, identity.user_email, identity.role, "confirm", request_id),
        {"token": token},
    )
    if resp.status_code == 409:
        raise ConfirmationError(resp.json().get("error", "Confirmation failed."))
    if resp.status_code != 200:
        raise TenantServiceUnavailable(f"Tenant service error ({resp.status_code}).")
    return resp.json()["message"]


def tenant_service_ready() -> bool:
    try:
        return httpx.get(f"{get_settings().tenant_mcp_base_url}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


# --- Agent toolset ---


async def _process_tool_call(ctx: RunContext[RunDeps], call_tool: CallToolFunc, name: str, args: dict[str, Any]) -> Any:
    """Enforce the per-request allowlist, log, and record ground-truth actions and confirmations."""
    if name not in ctx.deps.allowed_tools:  # defence in depth; the filter already hides it
        return "You are not permitted to do that."
    log.info("[%s] %s(%s)", ctx.deps.request_id, name, redact(", ".join(f"{k}={v!r}" for k, v in args.items())))
    result = await call_tool(name, args)
    if isinstance(result, dict):
        if result.get("confirmation_required"):
            pending = PendingConfirmation(
                token=result["token"], tool=name, summary=result["summary"], expires_in=result["expires_in"]
            )
            ctx.deps.confirmations.append(pending)
            # The model never sees the token
            return f"Confirmation required (not done yet): {pending.summary} The user must confirm before it happens."
        if result.get("changed") is True:
            ctx.deps.actions.append(Action(tool=name, message=str(result.get("message", name)), result=result))
    return result


def _allowed(ctx: RunContext[RunDeps], tool_def: ToolDefinition) -> bool:
    return tool_def.name in ctx.deps.allowed_tools


def build_toolset(deps: RunDeps) -> AbstractToolset[RunDeps]:
    """A per-run MCP connection carrying this request's signed context, filtered to the allowed tools."""
    identity = deps.identity
    toolset: MCPToolset[RunDeps] = MCPToolset(
        get_settings().tenant_mcp_url,
        headers=_headers(identity.tenant_id, identity.user_email, identity.role, "agent", deps.request_id),
        process_tool_call=_process_tool_call,
        tool_error_behavior="retry",
        max_retries=2,
    )
    return toolset.filtered(_allowed)
