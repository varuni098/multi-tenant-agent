"""graph-mcp: read-only knowledge-graph tools over MCP, plus the can_apply route for the write gate.

    uvicorn app.graph_mcp.server:app --port 8002

Holds Neo4j credentials only; it never touches MySQL. Signed contexts from the agent app
are trusted for identity (the app verified membership with tenant-mcp at login); external
bearer callers are checked against the graph's User/Role nodes.
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
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.graph import queries, service
from app.graph.client import get_driver, ping
from app.harness.context import Identity
from app.harness.guardrails import redact
from app.harness.policy import TOOL_POLICIES, role_allows
from app.knowledge import retrieval
from app.knowledge.embeddings import EmbeddingUnavailable, get_embedder
from app.mcp_server.principal import (
    AuthFailed,
    AuthMiddleware,
    Principal,
    capped,
    check_purpose,
    parse_credentials,
)
from app.services.errors import DomainError

setup_logging()
log = logging.getLogger(__name__)

GRAPH_PATH_PURPOSES = {"/internal/can_apply": {"agent"}}

mcp = FastMCP(
    "graph-tools",
    instructions=(
        "Explain and check one tenant's configuration using the knowledge graph: why a setting has its "
        "value, whether a change is allowed (and which rule decides), and what a template switch would change."
    ),
    mask_error_details=True,
)


def resolve_graph_principal(context_header: str | None, authorization: str | None, path: str) -> Principal:
    c = parse_credentials(context_header, authorization)
    check_purpose(c.purpose, path, GRAPH_PATH_PURPOSES)
    driver = get_driver()
    tenant = queries.tenant_facts(driver, c.tenant_id)
    if tenant is not None and tenant["status"] != "active":
        raise AuthFailed(403, "Tenant is inactive")
    role = c.role
    if c.purpose == "external":
        graph_role = queries.member_role(driver, c.tenant_id, c.email)
        if graph_role is None:
            raise AuthFailed(403, "User is not a member of this tenant")
        role = capped(c.role, graph_role)
    return Principal(Identity(c.tenant_id, c.email, role), c.purpose, c.request_id)  # type: ignore[arg-type]


def _principal() -> Principal:
    return get_http_request().state.principal


def tool(fn: Callable[..., Any]) -> Any:
    name = fn.__name__
    assert TOOL_POLICIES[name].server == "graph"

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> Any:
        p = _principal()
        started = time.perf_counter()
        outcome = "ok"
        try:
            if not role_allows(p.identity.role, name):
                outcome = "denied"
                raise ToolError(f"You are not permitted to use {name}.")
            try:
                return fn(**kwargs)
            except DomainError as e:
                outcome = "error"
                raise ToolError(str(e)) from e
        finally:
            log.info(
                "[%s] %s %s %s(%s) -> %s %dms",
                p.request_id,
                p.purpose,
                p.identity.user_email,
                name,
                redact(repr(kwargs)),
                outcome,
                (time.perf_counter() - started) * 1000,
            )

    annotations = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
    return mcp.tool(name=name, annotations=annotations, run_in_thread=True)(wrapper)


def _tid() -> str:
    return _principal().identity.tenant_id


@tool
def explain_setting(key: str, page_name: str | None = None) -> dict[str, Any]:
    """Explain why a setting has its value: the inheritance chain page > tenant > template > default.

    Args:
        key: Setting name, e.g. 'theme'.
        page_name: The page's short identifier, or omit for the tenant-wide value.
    """
    return service.explain(_tid(), key, page_name)


@tool
def can_apply(key: str, value: str, action: str = "set_setting") -> dict[str, Any]:
    """Check whether a change is allowed before making it. Returns the deciding ontology rule and why.

    Args:
        key: Setting name, e.g. 'brand_color'.
        value: Proposed value.
        action: The tool that would make the change: 'set_setting', 'set_theme' or 'bulk_set_setting'.
    """
    p = _principal()
    return service.can_apply(p.identity.tenant_id, p.identity.role, action, key, value).as_dict()


@tool
def impact_of_change(template_name: str, key: str | None = None) -> dict[str, Any]:
    """Preview which of this tenant's effective settings would change if it switched to another template.

    Args:
        template_name: The template to compare against, e.g. 'dark_first'.
        key: Limit to one setting, or omit for all.
    """
    return service.impact_of_change(_tid(), template_name, key)


@tool
def graph_effective_settings(page_name: str | None = None) -> list[dict[str, Any]]:
    """Every effective setting of a page (or tenant-wide), computed from the graph.

    Args:
        page_name: The page's short identifier, or omit for tenant-wide values.
    """
    return service.effective_settings(_tid(), page_name)


@tool
def resolve_entity(text: str) -> list[dict[str, Any]]:
    """Map loose wording ('accent colour', 'login screen', 'dark first') to settings, pages and templates.

    Args:
        text: The words to resolve.
    """
    return service.resolve_entity(_tid(), text)


@tool
def find_related(entity_type: str, name: str, depth: int = 1) -> list[dict[str, Any]]:
    """Neighbours of an entity in this tenant's graph (1 or 2 hops).

    Args:
        entity_type: 'page', 'setting', 'template', 'plan' or 'feature'.
        name: The entity's name.
        depth: 1 or 2.
    """
    return service.related(_tid(), entity_type, name, depth)


@tool
def search_docs(query: str) -> list[dict[str, Any]]:
    """Search the product documentation (and this tenant's own docs) for how-to and policy questions.
    Returns excerpts with citations like "[1] Templates › Switching templates"; cite them in the answer.

    Args:
        query: The question or topic, in natural language.
    """
    try:
        return retrieval.search(get_driver(), get_embedder(), _tid(), query, get_settings().search_top_k)
    except (retrieval.IndexNotReady, EmbeddingUnavailable) as e:
        raise ToolError(str(e)) from e


# --- Routes ---


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    try:
        await anyio.to_thread.run_sync(ping, get_driver())
        return JSONResponse({"status": "ok", "graph": True})
    except Exception:
        return JSONResponse({"status": "degraded", "graph": False}, status_code=503)


@mcp.custom_route("/internal/can_apply", methods=["POST"])
async def can_apply_route(request: Request) -> JSONResponse:
    """The harness write gate (purpose: agent). Uses the caller's role from the signed context."""
    p: Principal = request.state.principal
    body = await request.json()

    def run() -> dict[str, Any]:
        return service.can_apply(
            p.identity.tenant_id,
            p.identity.role,
            str(body.get("action", "")),
            str(body.get("key", "")),
            body.get("value"),
        ).as_dict()

    decision = await anyio.to_thread.run_sync(run)
    log.info(
        "[%s] gate %s %s=%r -> %s",
        p.request_id,
        body.get("action"),
        body.get("key"),
        body.get("value"),
        decision["rule"] or "allowed",
    )
    return JSONResponse(decision)


app = mcp.http_app(
    path="/mcp",
    stateless_http=True,
    json_response=True,
    middleware=[Middleware(AuthMiddleware, resolver=resolve_graph_principal)],
)
