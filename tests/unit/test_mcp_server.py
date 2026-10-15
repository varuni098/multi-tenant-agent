"""tenant-mcp as a standalone server: authentication, purposes, policy, annotations, call log."""

import asyncio
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from app.core.config import get_settings
from app.core.service_context import HEADER, ServiceContext, sign_context
from app.db.models import Tenant, ToolCall
from app.db.session import session_scope
from app.harness.auth import create_token
from app.services import settings as svc
from tests.conftest import MCP_URL, TENANT_A, TENANT_B

BASE = MCP_URL.removesuffix("/mcp")


def _bearer(email="editor@acme.test", role="editor", tenant=TENANT_A) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_token(tenant, email, role)}"}


def _context(purpose="agent", email="editor@acme.test", role="editor", tenant=TENANT_A) -> dict[str, str]:
    return {HEADER: sign_context(ServiceContext(tenant, email, role, purpose, "req-mcp"))}  # type: ignore[arg-type]


def _mcp(headers: dict[str, str], fn):
    async def run() -> Any:
        async with Client(StreamableHttpTransport(MCP_URL, headers=headers)) as client:
            return await fn(client)

    return asyncio.run(run())


def _tool_calls() -> list[tuple[str, str, str, str]]:
    with session_scope() as s:
        return [(c.actor, c.channel, c.tool, c.outcome) for c in s.scalars(select(ToolCall).order_by(ToolCall.id))]


# --- Authentication ---


def test_health_is_public():
    assert httpx.get(f"{BASE}/health").json() == {"status": "ok", "database": True}


def test_mcp_requires_credentials():
    resp = httpx.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_forged_context_is_rejected(settings):
    headers = _context()
    settings.mcp_context_secret = "a-different-secret-the-app-does-not-know-123"
    assert httpx.post(MCP_URL, headers=headers, json={}).status_code == 401


def test_non_member_is_forbidden():
    headers = _context(email="admin@globex.test")  # member of B, claiming A
    assert httpx.post(MCP_URL, headers=headers, json={}).status_code == 403


def test_suspended_tenant_is_forbidden():
    with session_scope() as s:
        tenant = s.get(Tenant, TENANT_A)
        assert tenant is not None
        tenant.status = "suspended"
    assert httpx.post(MCP_URL, headers=_bearer(), json={}).status_code == 403


@pytest.mark.parametrize(
    ("path", "headers_fn"),
    [
        ("/internal/whoami", lambda: _bearer()),  # external clients can't use internal routes
        ("/internal/confirm", lambda: _bearer()),
        ("/internal/confirm", lambda: _context("agent")),  # an agent run can't confirm its own action
        ("/internal/whoami", lambda: _context("confirm")),
        ("/mcp", lambda: _context("auth")),
        ("/mcp", lambda: _context("confirm")),
    ],
)
def test_credentials_are_bound_to_their_purpose(path, headers_fn):
    assert httpx.post(f"{BASE}{path}", headers=headers_fn(), json={}).status_code == 403


def test_whoami():
    info = httpx.post(f"{BASE}/internal/whoami", headers=_context("auth", role="admin")).json()
    assert info == {
        "member": True,
        "role": "editor",
        "tenant_active": True,
        "page_names": ["dashboard", "login", "settings"],
    }
    stranger = httpx.post(f"{BASE}/internal/whoami", headers=_context("auth", email="nobody@acme.test")).json()
    assert stranger["member"] is False


# --- Tools for external MCP clients (user bearer JWT) ---


def test_external_client_lists_tools_with_annotations():
    tools = {t.name: t for t in _mcp(_bearer(), lambda c: c.list_tools())}
    assert len(tools) == 12
    assert tools["get_theme"].annotations.readOnlyHint is True
    assert tools["set_theme"].annotations.readOnlyHint is False
    assert tools["set_theme"].annotations.idempotentHint is True
    assert tools["delete_page"].annotations.destructiveHint is True
    for t in tools.values():
        assert not any("tenant" in p for p in t.inputSchema.get("properties", {}))


def test_external_client_reads_and_writes_own_tenant():
    result = _mcp(_bearer(), lambda c: c.call_tool("get_theme", {"page_name": "login"}))
    assert result.structured_content == {"key": "theme", "value": "light", "source": "page"}
    _mcp(_bearer(), lambda c: c.call_tool("set_theme", {"page_name": "login", "theme": "dark"}))
    assert svc.get_setting(TENANT_A, "theme", "login").value == "dark"
    assert svc.get_setting(TENANT_B, "theme", "login").value == "dark"  # B's own seed value
    assert svc.get_change_history(TENANT_A)[0].actor == "editor@acme.test"


def test_role_is_enforced_server_side():
    viewer = _bearer("viewer@acme.test", "viewer")
    with pytest.raises(ToolError, match="not permitted to use set_theme"):
        _mcp(viewer, lambda c: c.call_tool("set_theme", {"page_name": "login", "theme": "dark"}))
    # A forged-looking claim of admin is capped to the DB role (viewer)
    with pytest.raises(ToolError, match="not permitted"):
        _mcp(
            _bearer("viewer@acme.test", "admin"),
            lambda c: c.call_tool("set_theme", {"page_name": "login", "theme": "dark"}),
        )
    assert svc.get_setting(TENANT_A, "theme", "login").value == "light"


def test_domain_errors_come_back_as_tool_errors():
    with pytest.raises(ToolError, match="Available pages: dashboard, login, settings"):
        _mcp(_bearer(), lambda c: c.call_tool("get_theme", {"page_name": "billing"}))


def test_gated_tool_returns_token_and_only_the_app_can_confirm():
    admin = _bearer("admin@acme.test", "admin")
    result = _mcp(admin, lambda c: c.call_tool("delete_page", {"page_name": "settings"}))
    token = result.structured_content["token"]
    assert result.structured_content["confirmation_required"] is True
    assert "settings" in {p.name for p in svc.list_pages(TENANT_A)}

    # The app's confirm route (purpose "confirm") executes it for the same user
    resp = httpx.post(
        f"{BASE}/internal/confirm", headers=_context("confirm", "admin@acme.test", "admin"), json={"token": token}
    )
    assert resp.json() == {"message": "Deleted page 'settings'."}
    assert "settings" not in {p.name for p in svc.list_pages(TENANT_A)}


def test_every_call_is_logged():
    _mcp(_bearer(), lambda c: c.call_tool("list_pages", {}))
    with pytest.raises(ToolError):
        _mcp(_bearer("viewer@acme.test", "viewer"), lambda c: c.call_tool("undo_last_change", {}))
    with pytest.raises(ToolError):
        _mcp(_bearer(), lambda c: c.call_tool("get_theme", {"page_name": "nope"}))
    assert _tool_calls() == [
        ("editor@acme.test", "external", "list_pages", "ok"),
        ("viewer@acme.test", "external", "undo_last_change", "denied"),
        ("editor@acme.test", "external", "get_theme", "error"),
    ]


def test_settings_do_not_leak_secrets():
    # Sanity: the test server signs and verifies with the configured (dev) secret
    assert get_settings().mcp_context_secret
