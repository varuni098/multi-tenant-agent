"""Pure rule evaluation over facts read from the graph (no I/O, fully unit-testable)."""

import re
from dataclasses import dataclass, field
from typing import Any

from app.graph.ontology import load_ontology

_TRUE = {"true", "yes", "on", "1", "enabled"}
_FALSE = {"false", "no", "off", "0", "disabled"}


@dataclass
class Decision:
    allowed: bool
    rule: str | None  # the ontology rule that failed, None when allowed
    reason: str
    path: list[str] = field(default_factory=list)  # graph facts behind the decision
    normalized_value: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "rule": self.rule,
            "reason": self.reason,
            "path": self.path,
            "normalized_value": self.normalized_value,
        }


def check_value(key_def: dict[str, Any], value: Any) -> tuple[bool, Any, str]:
    """(ok, normalised value, what was expected) for a setting definition read from the graph."""
    vtype = key_def["value_type"]
    if vtype == "bool":
        if isinstance(value, bool):
            return True, value, ""
        text = str(value).strip().lower()
        if text in _TRUE:
            return True, True, ""
        if text in _FALSE:
            return True, False, ""
        return False, None, "true or false"
    text = str(value).strip()
    if vtype == "enum":
        allowed = key_def.get("allowed") or []
        for option in allowed:
            if text.lower() == str(option).lower():
                return True, option, ""
        return False, None, f"one of {', '.join(map(str, allowed))}"
    pattern = key_def.get("pattern")
    if pattern and not re.fullmatch(pattern, text):
        return False, None, f"a value matching {pattern}"
    return True, text, ""


def evaluate_can_apply(facts: dict[str, Any], action: str, key: str, value: Any) -> Decision:
    """Apply the ontology rules in order; the first failure is the decision."""
    o = load_ontology()
    tenant, role = facts.get("tenant"), facts["role"]

    def deny(rule_id: str, reason: str, path: list[str]) -> Decision:
        return Decision(False, rule_id, f"{reason} (rule '{rule_id}': {o.rule(rule_id).description})", path)

    for rule in o.rules:
        if rule.id == "tenant_active":
            if not tenant or tenant["status"] != "active":
                return deny(
                    "tenant_active", "The tenant is not active", [f"Tenant.status={tenant and tenant['status']}"]
                )
        elif rule.id == "permission":
            if not facts.get("role_may"):
                return deny(
                    "permission",
                    f"Role '{role}' may not perform '{action}'",
                    [f"Role({role}) -MAY-> Action({action}) not found"],
                )
        elif rule.id == "known_setting":
            if facts.get("key_def") is None:
                known = ", ".join(facts.get("known_keys", []))
                return deny("known_setting", f"Unknown setting '{key}'. Known settings: {known}", [])
        elif rule.id == "allowed_value":
            ok, normalized, expected = check_value(facts["key_def"], value)
            if not ok:
                return deny("allowed_value", f"Invalid value {value!r} for '{key}'; expected {expected}", [])
        elif rule.id == "entitlement":
            feature = facts.get("required_feature")
            if feature and not facts.get("plan_includes"):
                plan = tenant["plan"] if tenant else None
                return deny(
                    "entitlement",
                    f"'{key}' requires the '{feature}' feature, which the '{plan}' plan does not include",
                    [
                        f"SettingKey({key}) -REQUIRES-> Feature({feature})",
                        f"Tenant -SUBSCRIBES_TO-> Plan({plan}) has no -INCLUDES-> Feature({feature})",
                    ],
                )

    _, normalized, _ = check_value(facts["key_def"], value)
    path = [f"Role({role}) -MAY-> Action({action})"]
    if facts.get("required_feature") and tenant:
        path.append(f"Plan({tenant['plan']}) -INCLUDES-> Feature({facts['required_feature']})")
    return Decision(True, None, f"'{key}' = {normalized!r} is allowed.", path, normalized)


def resolve_chain(levels: dict[str, Any], template_name: str | None) -> dict[str, Any]:
    """Pick the effective value from {page, tenant, template, default} in ontology order."""
    chain = []
    effective = source = None
    for level in load_ontology().inheritance:
        present = levels.get(level) is not None
        chain.append({"level": level, "value": levels.get(level), "applies": False})
        if present and source is None:
            effective, source = levels[level], level
            chain[-1]["applies"] = True
    return {"value": effective, "source": source, "template": template_name, "chain": chain}


def explain_text(key: str, page_name: str | None, resolved: dict[str, Any]) -> str:
    where = f"page '{page_name}'" if page_name else "the tenant default"
    source, value = resolved["source"], resolved["value"]
    reasons = {
        "page": "it is set directly on the page",
        "tenant": "the page has no override, so it uses the tenant-wide default",
        "template": f"nothing overrides it, so it comes from the '{resolved['template']}' template",
        "default": "nothing sets it, so it uses the setting's built-in default",
    }
    shadowed = [
        f"{c['level']}={c['value']!r}" for c in resolved["chain"] if c["value"] is not None and not c["applies"]
    ]
    text = f"'{key}' for {where} is {value!r}: {reasons[source]}."
    if shadowed:
        text += f" It overrides: {', '.join(shadowed)}."
    return text


def template_switch_impact(
    pages: dict[str | None, dict[str, dict[str, Any]]],
    current_template: dict[str, Any],
    candidate_template: dict[str, Any],
    defaults: dict[str, Any],
) -> list[dict[str, Any]]:
    """Which effective values change if the tenant switches template.

    pages maps page name (None = tenant level) to {key: {"page": v, "tenant": v}} overrides.
    """
    changes = []
    for page_name, overrides in sorted(pages.items(), key=lambda kv: (kv[0] is not None, kv[0] or "")):
        for key in sorted(defaults):
            levels = overrides.get(key, {})
            if levels.get("page") is not None or levels.get("tenant") is not None:
                continue  # overridden: the template doesn't reach it
            before = current_template.get(key, defaults[key])
            after = candidate_template.get(key, defaults[key])
            if before != after:
                changes.append({"page": page_name, "key": key, "from": before, "to": after})
    return changes
