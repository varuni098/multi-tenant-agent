"""Knowledge-graph logic that needs no Neo4j: ontology, projection shape, validator, drift, rules, gate."""

import copy

import pytest
from pydantic_ai.messages import ToolReturnPart

from app.db.session import session_scope
from app.graph import rules, snapshot
from app.graph.drift import diff
from app.graph.model import Graph, Rel
from app.graph.ontology import load_ontology, parse_ontology
from app.graph.validate import validate
from app.harness import tenant_client
from app.harness.policy import allowed_tools, server_tools
from app.services import settings as svc
from app.workflow.graph import run_workflow
from tests.conftest import EDITOR_A, TENANT_A, TENANT_B, caller_for


def _expected() -> Graph:
    with session_scope() as s:
        return snapshot.full_graph(s)


# --- Ontology ---


def test_ontology_loads():
    o = load_ontology()
    assert o.version == 2
    assert {"Tenant", "Page", "Setting", "SettingKey", "Template", "Plan", "Feature", "Role", "Action"} <= set(
        o.classes
    )
    assert [r.id for r in o.rules] == ["tenant_active", "permission", "known_setting", "allowed_value", "entitlement"]
    assert o.inheritance == ("page", "tenant", "template", "default")
    assert "accent colour" in o.aliases["brand_color"]


def test_constraints_cover_every_class():
    statements = load_ontology().constraint_statements()
    assert "CREATE CONSTRAINT page_uid IF NOT EXISTS FOR (n:Page) REQUIRE n.uid IS UNIQUE" in statements
    assert len(statements) == len(load_ontology().classes)


def test_ontology_rejects_unsafe_identifiers():
    bad = {
        "version": 1,
        "classes": {"Page) DETACH DELETE n //": {"key": "uid"}},
        "relationships": [],
        "rules": [],
        "inheritance": [],
    }
    with pytest.raises(ValueError, match="Invalid ontology identifier"):
        parse_ontology(bad)


# --- Projection conforms to the ontology ---


def test_projection_of_seed_data_is_valid():
    assert validate(_expected()) == []


def test_projection_contents():
    g = _expected()
    assert ("Page", f"{TENANT_A}:login") in g.nodes
    assert g.nodes[("Setting", f"{TENANT_A}:login:theme")]["value_json"] == '"light"'
    assert Rel("SUBSCRIBES_TO", ("Tenant", TENANT_B), ("Plan", "pro")) in g.rels
    assert Rel("REQUIRES", ("SettingKey", "brand_color"), ("Feature", "custom_branding")) in g.rels
    assert Rel("MAY", ("Role", "editor"), ("Action", "set_theme")) in g.rels
    assert Rel("MAY", ("Role", "viewer"), ("Action", "set_theme")) not in g.rels


def test_projection_tracks_changes_and_history():
    svc.set_setting(TENANT_A, "theme", "dark")  # tenant-wide default
    g = _expected()
    assert g.nodes[("Setting", f"{TENANT_A}:*:theme")]["scope"] == "tenant"
    assert Rel("HAS_SETTING", ("Tenant", TENANT_A), ("Setting", f"{TENANT_A}:*:theme")) in g.rels
    events = [p for (label, _), p in g.nodes.items() if label == "ChangeEvent"]
    assert events and events[0]["action"] == "set_setting"
    assert validate(g) == []


# --- Validator catches violations ---


def _broken(mutate) -> list[str]:
    g = copy.deepcopy(_expected())
    mutate(g)
    return validate(g)


def test_validator_catches_cross_tenant_edge():
    errors = _broken(lambda g: g.add_rel("OWNS", ("Tenant", TENANT_B), ("Page", f"{TENANT_A}:login")))
    assert any("crosses tenants" in e for e in errors)


def test_validator_catches_unknown_label_and_relationship():
    def mutate(g):
        g.nodes[("Hacker", "x")] = {"id": "x"}
        g.add_rel("OWNS", ("Plan", "free"), ("Page", f"{TENANT_A}:login"))

    errors = _broken(mutate)
    assert any("label is not in the ontology" in e for e in errors)
    assert any("not allowed between Plan and Page" in e for e in errors)


def test_validator_catches_missing_property_and_cardinality():
    def mutate(g):
        del g.nodes[("Page", f"{TENANT_A}:login")]["name"]
        g.rels.discard(Rel("SUBSCRIBES_TO", ("Tenant", TENANT_A), ("Plan", "free")))

    errors = _broken(mutate)
    assert any("missing required properties ['name']" in e for e in errors)
    assert any("outgoing SUBSCRIBES_TO->Plan, expected 1" in e for e in errors)


def test_validator_catches_invalid_value_and_shared_node_with_tenant():
    def mutate(g):
        g.nodes[("Setting", f"{TENANT_A}:login:theme")]["value_json"] = '"purple"'
        g.nodes[("Plan", "free")]["tenant_id"] = TENANT_A

    errors = _broken(mutate)
    assert any('value "purple" is invalid for theme' in e for e in errors)
    assert any("shared node must not carry tenant_id" in e for e in errors)


# --- Drift ---


def test_drift_is_zero_for_identical_graphs():
    assert diff(_expected(), _expected()).total == 0


def test_drift_detects_missing_extra_and_changed():
    expected = _expected()
    actual = copy.deepcopy(expected)
    del actual.nodes[("Page", f"{TENANT_A}:settings")]
    actual.nodes[("Page", "stray")] = {"uid": "stray", "tenant_id": TENANT_A, "name": "stray"}
    actual.nodes[("Setting", f"{TENANT_A}:login:theme")]["value_json"] = '"dark"'
    actual.nodes[("DocChunk", "d1")] = {"uid": "d1", "text": "unmanaged"}  # ignored
    d = diff(expected, actual)
    assert d.missing_nodes == [("Page", f"{TENANT_A}:settings")]
    assert d.extra_nodes == [("Page", "stray")]
    assert [c[0] for c in d.changed_nodes] == [("Setting", f"{TENANT_A}:login:theme")]


# --- Rules (pure) ---

THEME = {"value_type": "enum", "allowed": ["light", "dark"], "pattern": None, "default": "light", "feature": None}
BRAND = {
    "value_type": "string",
    "allowed": None,
    "pattern": r"^#[0-9a-fA-F]{6}$",
    "default": "#2563eb",
    "feature": "custom_branding",
}


def _facts(**overrides):
    facts = {
        "tenant": {"status": "active", "plan": "free"},
        "role": "editor",
        "role_may": True,
        "key_def": THEME,
        "known_keys": ["brand_color", "theme"],
        "required_feature": None,
        "plan_includes": False,
    }
    facts.update(overrides)
    return facts


def test_can_apply_allows_and_normalises():
    d = rules.evaluate_can_apply(_facts(), "set_theme", "theme", " Dark ")
    assert (d.allowed, d.rule, d.normalized_value) == (True, None, "dark")


@pytest.mark.parametrize(
    ("overrides", "value", "rule"),
    [
        ({"tenant": {"status": "suspended", "plan": "free"}}, "dark", "tenant_active"),
        ({"role_may": False, "role": "viewer"}, "dark", "permission"),
        ({"key_def": None}, "dark", "known_setting"),
        ({}, "purple", "allowed_value"),
        ({"key_def": BRAND, "required_feature": "custom_branding"}, "#ff0000", "entitlement"),
    ],
)
def test_can_apply_denials_cite_the_rule(overrides, value, rule):
    d = rules.evaluate_can_apply(_facts(**overrides), "set_setting", "theme", value)
    assert not d.allowed and d.rule == rule
    assert f"rule '{rule}'" in d.reason


def test_entitlement_denial_shows_the_graph_path():
    d = rules.evaluate_can_apply(
        _facts(key_def=BRAND, required_feature="custom_branding"), "set_setting", "brand_color", "#ff0000"
    )
    assert d.path == [
        "SettingKey(brand_color) -REQUIRES-> Feature(custom_branding)",
        "Tenant -SUBSCRIBES_TO-> Plan(free) has no -INCLUDES-> Feature(custom_branding)",
    ]
    allowed = rules.evaluate_can_apply(
        _facts(
            key_def=BRAND,
            required_feature="custom_branding",
            plan_includes=True,
            tenant={"status": "active", "plan": "pro"},
        ),
        "set_setting",
        "brand_color",
        "#ff0000",
    )
    assert allowed.allowed and "Plan(pro) -INCLUDES-> Feature(custom_branding)" in allowed.path


def test_resolve_chain_and_explanation():
    resolved = rules.resolve_chain({"page": None, "tenant": None, "template": "light", "default": "light"}, "standard")
    assert (resolved["value"], resolved["source"]) == ("light", "template")
    text = rules.explain_text("theme", "settings", resolved)
    assert "comes from the 'standard' template" in text
    page = rules.resolve_chain({"page": "dark", "tenant": None, "template": "light", "default": "light"}, "standard")
    assert "It overrides: template='light', default='light'" in rules.explain_text("theme", "dashboard", page)


def test_template_switch_impact_skips_overridden_values():
    pages = {None: {}, "login": {"theme": {"page": "light"}}, "settings": {}}
    changes = rules.template_switch_impact(
        pages,
        {"theme": "light", "layout": "comfortable"},
        {"theme": "dark", "layout": "compact"},
        {"theme": "light", "layout": "comfortable"},
    )
    assert {"page": "settings", "key": "theme", "from": "light", "to": "dark"} in changes
    assert not any(c["page"] == "login" and c["key"] == "theme" for c in changes)
    assert {"page": "login", "key": "layout", "from": "comfortable", "to": "compact"} in changes


# --- Policy ---


def test_graph_tools_only_when_enabled():
    assert not {"explain_setting", "can_apply"} & allowed_tools("admin", "write")
    with_graph = allowed_tools("admin", "read", graph=True)
    assert {"explain_setting", "can_apply", "impact_of_change"} <= with_graph
    assert not {"resolve_entity", "find_related", "graph_effective_settings"} & with_graph  # external clients only
    assert "impact_of_change" not in allowed_tools("editor", "read", graph=True)
    assert len(server_tools("graph")) == 7 and len(server_tools("tenant")) == 12


# --- Write gate (harness) with a stubbed policy engine ---


@pytest.fixture
def gate(monkeypatch, settings):
    settings.graph_mode = "enforce"
    calls: list[tuple] = []
    decisions: dict[str, dict | None] = {}

    async def fake_gate_check(deps, action, key, value):
        calls.append((action, key, value))
        return decisions.get(key, {"allowed": True, "rule": None, "reason": "ok"})

    monkeypatch.setattr(tenant_client, "gate_check", fake_gate_check)
    return calls, decisions


def test_gate_blocks_denied_write_before_it_reaches_tenant_mcp(script, gate):
    calls, decisions = gate
    decisions["brand_color"] = {
        "allowed": False,
        "rule": "entitlement",
        "reason": "needs custom_branding (rule 'entitlement')",
    }
    s = script([("set_setting", {"key": "brand_color", "value": "#ff0000", "page_name": "login"})], "Not possible.")
    result = run_workflow("set the brand color of login to #ff0000", caller_for(EDITOR_A), "req", "s")
    assert calls == [("set_setting", "brand_color", "#ff0000")]
    assert [(d.tool, d.rule) for d in result.denials] == [("set_setting", "entitlement")]
    assert result.actions == []
    tool_reply = [
        p
        for m in s.history[1]
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolReturnPart) and p.tool_name == "set_setting"
    ]
    assert "Not allowed, nothing was changed" in str(tool_reply[0].content)


def test_gate_allows_permitted_write(script, gate):
    calls, _ = gate
    script([("set_theme", {"page_name": "login", "theme": "dark"})], "Done.")
    result = run_workflow("make login dark", caller_for(EDITOR_A), "req", "s")
    assert calls == [("set_theme", "theme", "dark")]
    assert [a.tool for a in result.actions] == ["set_theme"]
    assert svc.get_setting(TENANT_A, "theme", "login").value == "dark"


def test_gate_fails_closed_when_policy_engine_is_down(script, gate):
    _, decisions = gate
    decisions["theme"] = None  # engine unavailable
    script([("set_theme", {"page_name": "login", "theme": "dark"})], "Sorry.")
    result = run_workflow("make login dark", caller_for(EDITOR_A), "req", "s")
    assert [d.rule for d in result.denials] == ["policy_unavailable"]
    assert svc.get_setting(TENANT_A, "theme", "login").value == "light"


def test_advisory_mode_records_but_does_not_block(script, gate, settings):
    settings.graph_mode = "advisory"
    _, decisions = gate
    decisions["theme"] = {"allowed": False, "rule": "permission", "reason": "nope"}
    script([("set_theme", {"page_name": "login", "theme": "dark"})], "Done.")
    result = run_workflow("make login dark", caller_for(EDITOR_A), "req", "s")
    assert [d.rule for d in result.denials] == ["permission"]
    assert svc.get_setting(TENANT_A, "theme", "login").value == "dark"


def test_reads_are_never_gated(script, gate):
    calls, _ = gate
    script([("get_theme", {"page_name": "login"})], "light")
    run_workflow("theme of login?", caller_for(EDITOR_A), "req", "s")
    assert calls == []
