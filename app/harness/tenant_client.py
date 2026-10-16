"""The agent app's only route to tenant data: tenant-mcp, over MCP and two internal routes."""

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic_ai import RunContext
from pydantic_ai.mcp import CallToolFunc, MCPToolset
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset

from app.core.config import get_settings
from app.core.service_context import HEADER, Purpose, ServiceContext, sign_context
from app.harness.context import Action, Citation, Denial, Identity, PendingConfirmation, RunDeps
from app.harness.guardrails import redact
from app.harness.policy import TOOL_POLICIES

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


def _api_post(identity: Identity, path: str, request_id: str, body: dict[str, Any] | None = None) -> Any:
    """Read-only REST views for the UI (purpose api). Returns the JSON body; raises on errors."""
    resp = _post(path, _headers(identity.tenant_id, identity.user_email, identity.role, "api", request_id), body)
    if resp.status_code == 403:
        raise PermissionError(resp.json().get("error", "Forbidden."))
    if resp.status_code != 200:
        raise TenantServiceUnavailable(f"Tenant service error ({resp.status_code}).")
    return resp.json()


def settings_view(identity: Identity, request_id: str) -> dict[str, Any]:
    return _api_post(identity, "/internal/settings", request_id)


def audit(identity: Identity, request_id: str, query: dict[str, Any]) -> list[dict[str, Any]]:
    return _api_post(identity, "/internal/audit", request_id, query)["items"]


def tenant_service_ready() -> bool:
    try:
        return httpx.get(f"{get_settings().tenant_mcp_base_url}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


# --- Agent toolset ---


# Write tools the policy gate checks, and the (key, value) pairs each would set
GATED_WRITES: dict[str, Callable[[dict[str, Any]], list[tuple[Any, Any]]]] = {
    "set_theme": lambda a: [("theme", a.get("theme"))],
    "set_setting": lambda a: [(a.get("key"), a.get("value"))],
    "bulk_set_setting": lambda a: [(a.get("key"), a.get("value"))],
    "create_page": lambda a: [("theme", a["theme"])] if a.get("theme") is not None else [],
}


async def gate_check(deps: RunDeps, action: str, key: Any, value: Any) -> dict[str, Any] | None:
    """Ask graph-mcp whether a change is allowed. None = the policy engine couldn't answer."""
    identity = deps.identity
    headers = _headers(identity.tenant_id, identity.user_email, identity.role, "agent", deps.request_id)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{get_settings().graph_mcp_base_url}/internal/can_apply",
                headers=headers,
                json={"action": action, "key": key, "value": value},
            )
    except httpx.HTTPError:
        return None
    return resp.json() if resp.status_code == 200 else None


async def _gate(ctx: RunContext[RunDeps], name: str, args: dict[str, Any]) -> str | None:
    """Return a refusal for the model if the policy gate blocks this write, else None."""
    mode = get_settings().graph_mode
    if mode == "off" or name not in GATED_WRITES:
        return None
    for key, value in GATED_WRITES[name](args):
        decision = await gate_check(ctx.deps, name, key, value)
        if decision is None:
            log.warning("[%s] policy engine unavailable for %s (%s mode)", ctx.deps.request_id, name, mode)
            if mode == "enforce":
                ctx.deps.denials.append(Denial(name, "policy_unavailable", "The policy engine is unavailable."))
                return "The policy engine is unavailable, so nothing was changed. Ask the user to try again shortly."
            continue
        if not decision["allowed"]:
            ctx.deps.denials.append(Denial(name, decision["rule"], decision["reason"]))
            log.info("[%s] gate denied %s: %s", ctx.deps.request_id, name, decision["rule"])
            if mode == "enforce":
                return f"Not allowed, nothing was changed: {decision['reason']}"
    return None


async def _process_tool_call(ctx: RunContext[RunDeps], call_tool: CallToolFunc, name: str, args: dict[str, Any]) -> Any:
    """Enforce the allowlist and policy gate, log, and record ground-truth actions and confirmations."""
    if name not in ctx.deps.allowed_tools:  # defence in depth; the filter already hides it
        return "You are not permitted to do that."
    log.info("[%s] %s(%s)", ctx.deps.request_id, name, redact(", ".join(f"{k}={v!r}" for k, v in args.items())))
    ctx.deps.event("tool_start", tool=name, args=_redacted(args))
    refusal = await _gate(ctx, name, args)
    if refusal:
        ctx.deps.event("tool_end", tool=name, outcome="denied", detail=refusal)
        return refusal
    try:
        result = await call_tool(name, args)
    except Exception:
        ctx.deps.event("tool_end", tool=name, outcome="error")
        raise
    if isinstance(result, dict):
        if result.get("confirmation_required"):
            pending = PendingConfirmation(
                token=result["token"], tool=name, summary=result["summary"], expires_in=result["expires_in"]
            )
            ctx.deps.confirmations.append(pending)
            ctx.deps.event("tool_end", tool=name, outcome="needs_confirmation", detail=pending.summary)
            # The model never sees the token
            return f"Confirmation required (not done yet): {pending.summary} The user must confirm before it happens."
        if result.get("changed") is True:
            message = str(result.get("message", name))
            ctx.deps.actions.append(Action(tool=name, message=message, result=result))
            ctx.deps.event("tool_end", tool=name, outcome="changed", detail=message)
            return result
    ctx.deps.event("tool_end", tool=name, outcome="ok")
    return result


def _redacted(args: dict[str, Any]) -> dict[str, Any]:
    return {k: redact(v) if isinstance(v, str) else v for k, v in args.items()}


async def _log_graph_call(ctx: RunContext[RunDeps], call_tool: CallToolFunc, name: str, args: dict[str, Any]) -> Any:
    if name not in ctx.deps.allowed_tools:
        return "You are not permitted to do that."
    log.info("[%s] graph %s(%s)", ctx.deps.request_id, name, redact(", ".join(f"{k}={v!r}" for k, v in args.items())))
    ctx.deps.event("tool_start", tool=name, args=_redacted(args))
    try:
        result = await call_tool(name, args)
    except Exception:
        ctx.deps.event("tool_end", tool=name, outcome="error")
        raise
    if name == "search_docs":
        hits = result.get("result", []) if isinstance(result, dict) else result
        for hit in hits if isinstance(hits, list) else []:
            if isinstance(hit, dict) and {"rank", "title", "heading", "path"} <= hit.keys():
                ctx.deps.citations.append(Citation(hit["rank"], hit["title"], hit["heading"], hit["path"]))
    ctx.deps.event("tool_end", tool=name, outcome="ok")
    return result


def _allowed(ctx: RunContext[RunDeps], tool_def: ToolDefinition) -> bool:
    return tool_def.name in ctx.deps.allowed_tools


def _toolset(url: str, deps: RunDeps, hook: Any) -> AbstractToolset[RunDeps]:
    identity = deps.identity
    toolset: MCPToolset[RunDeps] = MCPToolset(
        url,
        headers=_headers(identity.tenant_id, identity.user_email, identity.role, "agent", deps.request_id),
        process_tool_call=hook,
        tool_error_behavior="retry",
        max_retries=2,
    )
    return toolset.filtered(_allowed)


def build_toolsets(deps: RunDeps) -> list[AbstractToolset[RunDeps]]:
    """Per-run MCP connections carrying this request's signed context, filtered to the allowed tools."""
    toolsets = [_toolset(get_settings().tenant_mcp_url, deps, _process_tool_call)]
    if any(TOOL_POLICIES[t].server == "graph" for t in deps.allowed_tools):
        toolsets.append(_toolset(get_settings().graph_mcp_url, deps, _log_graph_call))
    return toolsets


_graph_health: dict[str, float | bool] = {"checked": 0.0, "ok": False}


def graph_available() -> bool:
    """Graph enabled and graph-mcp healthy (cached for 10 s so requests don't pay for the check)."""
    if get_settings().graph_mode == "off":
        return False
    now = time.monotonic()
    if now - float(_graph_health["checked"]) > 10:
        try:
            ok = httpx.get(f"{get_settings().graph_mcp_base_url}/health", timeout=2).status_code == 200
        except httpx.HTTPError:
            ok = False
        _graph_health.update(checked=now, ok=ok)
    return bool(_graph_health["ok"])
