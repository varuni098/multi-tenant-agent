"""Knowledge graph against real Neo4j and RabbitMQ.

    docker compose --profile test run --rm tests

Skipped unless RUN_INTEGRATION=1 (the compose test profile sets it).
"""

import json
import os
import time

import httpx
import pika
import pytest
from pydantic_ai.messages import ToolReturnPart

from app.core.service_context import HEADER, ServiceContext, sign_context
from app.db.models import Tenant
from app.db.session import session_scope
from app.graph import broker, service
from app.graph.client import dump_graph, ensure_constraints, get_driver
from app.graph.reconcile import compute_drift, project_all, project_tenant, reconcile
from app.graph.relay import publish_pending
from app.graph.sync import handle
from app.graph.validate import validate
from app.harness.auth import create_token
from app.services import admin
from app.services import settings as svc
from app.workflow.graph import run_workflow
from tests.conftest import (
    ADMIN_A,
    EDITOR_A,
    GRAPH_PORT,
    GRAPH_URL,
    TENANT_A,
    TENANT_B,
    VIEWER_A,
    caller_for,
    start_server,
)

pytestmark = pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="needs Neo4j and RabbitMQ")

BASE = GRAPH_URL.removesuffix("/mcp")


@pytest.fixture(scope="module")
def graph_server():
    from app.graph_mcp.server import app

    stop = start_server(app, GRAPH_PORT)
    yield
    stop()


@pytest.fixture(autouse=True)
def graph():
    """Empty graph, constraints, then a full projection of the freshly seeded test DB."""
    driver = get_driver()
    driver.execute_query("MATCH (n) DETACH DELETE n")
    ensure_constraints(driver)
    project_all(driver)
    return driver


def _ctx(identity, purpose="agent"):
    return {HEADER: sign_context(ServiceContext(identity.tenant_id, identity.user_email, identity.role, purpose, "it"))}


# --- Projection, validation, drift ---


def test_projected_graph_conforms_to_ontology(graph):
    assert validate(dump_graph(graph)) == []


def test_no_drift_after_projection(graph):
    assert compute_drift(graph).total == 0


def test_graph_effective_values_match_mysql_everywhere(graph):
    svc.set_setting(TENANT_A, "theme", "dark")  # tenant default
    svc.set_setting(TENANT_B, "brand_color", "#ff0000", "login")
    for tid in (TENANT_A, TENANT_B):
        project_tenant(graph, tid)
        for page in [None, *[p.name for p in svc.list_pages(tid)]]:
            expected = {s.key: (s.value, s.source) for s in svc.get_effective_settings(tid, page)}
            actual = {s["key"]: (s["value"], s["source"]) for s in service.effective_settings(tid, page)}
            assert actual == expected, (tid, page)


def test_drift_is_detected_and_repaired(graph):
    graph.execute_query("MATCH (s:Setting {uid: $uid}) DETACH DELETE s", uid=f"{TENANT_A}:login:theme")
    graph.execute_query("CREATE (:Page {uid: 'stray', tenant_id: $tid, name: 'stray'})", tid=TENANT_A)
    drift = reconcile(graph, fix=True)
    assert drift.missing_nodes and drift.extra_nodes
    assert compute_drift(graph).total == 0


# --- Graph tools ---


def test_explain_setting(graph):
    page = service.explain(TENANT_A, "theme", "dashboard")
    assert (page["value"], page["source"]) == ("dark", "page")
    inherited = service.explain(TENANT_A, "theme", "settings")
    assert inherited["source"] == "template" and "'standard' template" in inherited["explanation"]


@pytest.mark.parametrize(
    ("tenant", "role", "action", "key", "value", "rule"),
    [
        (TENANT_A, "editor", "set_setting", "brand_color", "#ff0000", "entitlement"),
        (TENANT_A, "viewer", "set_theme", "theme", "dark", "permission"),
        (TENANT_A, "editor", "set_theme", "theme", "purple", "allowed_value"),
        (TENANT_A, "editor", "set_setting", "font", "x", "known_setting"),
        (TENANT_B, "admin", "set_setting", "brand_color", "#ff0000", None),
        (TENANT_A, "editor", "set_theme", "theme", "Dark", None),
    ],
)
def test_can_apply(graph, tenant, role, action, key, value, rule):
    d = service.can_apply(tenant, role, action, key, value)
    assert d.allowed is (rule is None) and d.rule == rule


def test_can_apply_respects_tenant_status(graph):
    with session_scope() as s:
        tenant = s.get(Tenant, TENANT_A)
        assert tenant is not None
        tenant.status = "suspended"
    project_tenant(graph, TENANT_A)
    assert service.can_apply(TENANT_A, "admin", "set_theme", "theme", "dark").rule == "tenant_active"


def test_impact_of_template_switch(graph):
    impact = service.impact_of_change(TENANT_A, "dark_first")
    changes = {(c["page"], c["key"]): (c["from"], c["to"]) for c in impact["changes"]}
    assert changes[("settings", "theme")] == ("light", "dark")  # inherited, so it changes
    assert ("login", "theme") not in changes  # overridden on the page
    assert changes[("login", "layout")] == ("comfortable", "compact")


def test_resolve_entity(graph):
    found = {(f["type"], f["name"]) for f in service.resolve_entity(TENANT_A, "accent colour on the login screen")}
    assert {("setting", "brand_color"), ("page", "login")} <= found


def test_find_related_never_leaves_the_tenant(graph):
    for entity_type, name in [("page", "login"), ("setting", "theme"), ("template", "standard"), ("plan", "free")]:
        for depth in (1, 2):
            names = {str(n["name"]) for n in service.related(TENANT_A, entity_type, name, depth)}
            assert not any("globex" in n or TENANT_B in n for n in names), (entity_type, depth, names)


# --- Sync pipeline: outbox -> RabbitMQ -> worker -> Neo4j ---


@pytest.fixture
def channel():
    connection = broker.connect(retries=10)
    ch = connection.channel()
    ch.confirm_delivery()
    broker.declare(ch)
    ch.queue_purge(broker.QUEUE)
    ch.queue_purge(broker.DLQ)
    yield ch
    connection.close()


def _drain(ch) -> list[float]:
    lags = []
    while True:
        method, _props, body = ch.basic_get(broker.QUEUE)
        if method is None:
            return lags
        lags.append(handle(body) or 0.0)
        ch.basic_ack(method.delivery_tag)


def test_change_reaches_graph_within_five_seconds(graph, channel):
    started = time.monotonic()
    svc.set_theme(TENANT_A, "login", "dark")
    admin.create_page(TENANT_A, "billing", theme="dark")
    assert publish_pending(channel) >= 3  # set + create + its theme
    lags = _drain(channel)
    elapsed = time.monotonic() - started
    assert elapsed < 5 and max(lags) < 5
    assert service.explain(TENANT_A, "theme", "login")["value"] == "dark"
    assert service.explain(TENANT_A, "theme", "billing")["source"] == "page"
    assert compute_drift(graph).total == 0
    assert publish_pending(channel) == 0  # nothing left to publish


def test_redelivered_events_are_idempotent(graph, channel):
    svc.set_theme(TENANT_A, "login", "dark")
    publish_pending(channel)
    method, _p, body = channel.basic_get(broker.QUEUE)
    for _ in range(3):
        handle(body)
    channel.basic_ack(method.delivery_tag)
    assert compute_drift(graph).total == 0


def test_bad_message_goes_to_dead_letter_queue(channel):
    channel.basic_publish(broker.EXCHANGE, "junk", b"not json", properties=pika.BasicProperties(delivery_mode=2))
    from app.graph.sync import _on_message

    method, props, body = channel.basic_get(broker.QUEUE)
    _on_message(channel, method, props, body)
    time.sleep(0.5)
    dlq = channel.queue_declare(broker.DLQ, durable=True, passive=True)
    assert dlq.method.message_count == 1


# --- graph-mcp and the write gate end to end ---


def test_graph_mcp_can_apply_route(graph, graph_server):
    resp = httpx.post(
        f"{BASE}/internal/can_apply",
        headers=_ctx(EDITOR_A),
        json={"action": "set_setting", "key": "brand_color", "value": "#ff0000"},
    )
    body = resp.json()
    assert (body["allowed"], body["rule"]) == (False, "entitlement")
    assert any("REQUIRES" in step for step in body["path"])


def test_graph_mcp_external_client_membership(graph, graph_server):
    ok = {"Authorization": f"Bearer {create_token(TENANT_A, 'viewer@acme.test', 'admin')}"}
    resp = httpx.post(
        f"{BASE}/mcp",
        headers={**ok, "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "can_apply", "arguments": {"key": "theme", "value": "dark", "action": "set_theme"}},
        },
    )
    result = resp.json()["result"]["structuredContent"]
    assert result["rule"] == "permission"  # admin claim capped to the viewer role in the graph
    stranger = {"Authorization": f"Bearer {create_token(TENANT_B, 'admin@acme.test', 'admin')}"}
    assert httpx.post(f"{BASE}/mcp", headers=stranger, json={}).status_code == 403


def test_agent_write_is_gated_by_the_graph(graph, graph_server, script, settings):
    settings.graph_mode = "enforce"
    s = script([("set_setting", {"key": "brand_color", "value": "#ff0000", "page_name": "login"})], "Not allowed.")
    result = run_workflow("set brand colour of login to #ff0000", caller_for(EDITOR_A), "req", "s")
    assert [(d.rule) for d in result.denials] == ["entitlement"]
    assert svc.get_setting(TENANT_A, "brand_color", "login").source == "default"
    assert {"explain_setting", "can_apply"} <= set(s.offered[0])  # graph tools offered when healthy


def test_agent_can_use_graph_tools(graph, graph_server, script, settings):
    settings.graph_mode = "enforce"
    s = script([("explain_setting", {"key": "theme", "page_name": "settings"})], "It comes from the template.")
    run_workflow("why is the settings page light?", caller_for(VIEWER_A), "req", "s")
    returned = [
        p
        for m in s.history[1]
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolReturnPart) and p.tool_name == "explain_setting"
    ]
    assert "'standard' template" in json.dumps(returned[0].model_response_str())


def test_admin_only_graph_tool(graph, graph_server, script, settings):
    settings.graph_mode = "enforce"
    s = script("ok")
    run_workflow("what would change with the dark_first template?", caller_for(ADMIN_A), "req", "s")
    assert "impact_of_change" in s.offered[0]
    s2 = script("ok")
    run_workflow("what would change with the dark_first template?", caller_for(EDITOR_A), "req", "s2")
    assert "impact_of_change" not in s2.offered[0]
