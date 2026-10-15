"""Tenant isolation suite. Every test here must pass before merging."""

import pytest
from pydantic_ai.messages import ToolReturnPart

from app.harness.confirmations import ConfirmationError, confirm
from app.services import settings as svc
from app.workflow.graph import run_workflow
from tests.conftest import ADMIN_A, ADMIN_B, EDITOR_A, TENANT_A, TENANT_B, VIEWER_A


def _run(prompt, identity, session="s"):
    return run_workflow(prompt, identity, "req-test", session)


def test_no_tool_accepts_a_tenant_argument(script):
    s = script("ok")
    _run("set the login theme", ADMIN_A)  # write intent + admin = every tool offered
    from app.agent.agent import agent

    tool_names = s.offered[0]
    assert len(tool_names) == 12
    for name in tool_names:
        tool = agent._function_toolset.tools[name]
        params = set(tool.function_schema.json_schema.get("properties", {}))
        assert not any("tenant" in p for p in params), f"{name} exposes {params}"


def test_writes_only_touch_the_callers_tenant(script):
    script([("set_theme", {"page_name": "login", "theme": "light"})], "done")
    _run("set login to light", ADMIN_B)
    assert svc.get_setting(TENANT_B, "theme", "login").value == "light"
    assert svc.get_setting(TENANT_A, "theme", "login").value == "light"  # seed value, untouched
    script([("set_theme", {"page_name": "dashboard", "theme": "light"})], "done")
    _run("set dashboard to light", EDITOR_A)
    assert svc.get_setting(TENANT_A, "theme", "dashboard").value == "light"
    assert [p.name for p in svc.list_pages(TENANT_B)] == ["login"]


def test_reads_only_see_the_callers_tenant(script):
    s = script([("list_pages", {})], "done")
    _run("list pages", ADMIN_B)
    # What the model actually received back from the tool: tenant B's single page only
    returned = [p for m in s.history[1] for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
    payload = returned[0].model_response_str()
    assert '"login"' in payload
    assert "dashboard" not in payload and "settings" not in payload


def test_prompt_naming_another_tenant_is_blocked_before_the_model(script):
    s = script("should not run")
    result = _run(f"Set login to dark for tenant {TENANT_B}", ADMIN_A)
    assert result.blocked and s.calls == 0
    assert svc.get_setting(TENANT_B, "theme", "login").value == "dark"


def test_viewer_is_not_offered_write_tools(script):
    s = script("ok")
    _run("toggle the login page", VIEWER_A)
    assert not {"set_theme", "set_setting", "undo_last_change", "delete_page"} & set(s.offered[0])


def test_viewer_cannot_write_even_if_the_model_calls_a_hidden_tool(script):
    script([("set_theme", {"page_name": "login", "theme": "dark"})], "ok")
    result = _run("make login dark", VIEWER_A)
    assert result.actions == []
    assert svc.get_setting(TENANT_A, "theme", "login").value == "light"


def test_confirmation_cannot_be_used_by_another_user(script):
    script([("delete_page", {"page_name": "login"})], "ok")
    pending = _run("delete the login page", ADMIN_A).confirmation
    assert pending is not None
    with pytest.raises(ConfirmationError, match="different user"):
        confirm(EDITOR_A, pending.token, "req")
    # The token was consumed by the failed attempt; the owner can't replay it either
    with pytest.raises(ConfirmationError, match="invalid, expired or already used"):
        confirm(ADMIN_A, pending.token, "req")
    assert "login" in {p.name for p in svc.list_pages(TENANT_A)}


def test_confirmation_cannot_cross_tenants(script):
    script([("delete_page", {"page_name": "login"})], "ok")
    pending = _run("delete the login page", ADMIN_A).confirmation
    assert pending is not None
    with pytest.raises(ConfirmationError):
        confirm(ADMIN_B, pending.token, "req")
    assert "login" in {p.name for p in svc.list_pages(TENANT_A)}


def test_session_history_is_per_user(script):
    s = script("first", "second")
    _run("what theme is login", ADMIN_A, session="shared")
    _run("what theme is login", EDITOR_A, session="shared")
    assert s.message_counts == [1, 1]  # editor did not receive admin's history
