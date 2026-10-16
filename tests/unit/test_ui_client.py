"""The UI's API client: SSE parsing, error mapping and dev login (no Reflex runtime needed)."""

import httpx
import pytest

from agent_ui import api
from tests.conftest import TENANT_A


def test_sse_parser_handles_comments_multiline_and_defaults():
    p = api.SSEParser()
    lines = [": keep-alive", "", "event: tool_start", 'data: {"tool":', 'data: "set_theme"}', "", 'data: {"x": 1}', ""]
    out = [e for e in (p.feed(line) for line in lines) if e]
    assert out == [("tool_start", {"tool": "set_theme"}), ("message", {"x": 1})]


def test_api_errors_use_the_envelope_message():
    resp = httpx.Response(403, json={"error": {"code": "forbidden", "message": "Only admins.", "request_id": "r"}})
    with pytest.raises(api.ApiError, match="Only admins.") as e:
        api._raise_for(resp)
    assert e.value.status == 403
    with pytest.raises(api.ApiError, match="Bad Gateway"):
        api._raise_for(httpx.Response(502, request=httpx.Request("GET", "http://x")))


def test_dev_login_lists_seeded_users_and_is_dev_only(monkeypatch):
    users = {u.email: u for u in api.dev_users()}
    assert users["editor@acme.test"].tenant_id == TENANT_A and users["editor@acme.test"].role == "editor"
    assert api.dev_token(users["editor@acme.test"]).count(".") == 2
    monkeypatch.setenv("APP_ENV", "prod")
    with pytest.raises(PermissionError):
        api.dev_token(users["editor@acme.test"])
