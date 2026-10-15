"""Graph tool implementations: queries + pure rules, raising the same domain errors as the services."""

import re
from difflib import SequenceMatcher
from typing import Any

from app.graph import queries, rules
from app.graph.client import get_driver
from app.graph.ontology import load_ontology
from app.services.errors import PageNotFound, TemplateNotFound, TenantNotFound, UnknownSettingKey

# Entity types find_related may start from (labels are interpolated into Cypher, so allowlist them)
RELATABLE = {"page": "Page", "setting": "SettingKey", "template": "Template", "plan": "Plan", "feature": "Feature"}


def _levels(tid: str, page_name: str | None) -> tuple[str | None, dict[str, dict]]:
    driver = get_driver()
    found, template, levels = queries.setting_levels(driver, tid, page_name)
    if not levels:
        raise TenantNotFound(tid)
    if not found:
        raise PageNotFound(page_name or "", queries.page_names(driver, tid))
    return template, levels


def effective_settings(tid: str, page_name: str | None = None) -> list[dict[str, Any]]:
    template, levels = _levels(tid, page_name)
    out = []
    for key in sorted(levels):
        resolved = rules.resolve_chain(levels[key], template)
        out.append({"key": key, "value": resolved["value"], "source": resolved["source"]})
    return out


def explain(tid: str, key: str, page_name: str | None = None) -> dict[str, Any]:
    template, levels = _levels(tid, page_name)
    key = key.strip().lower()
    if key not in levels:
        raise UnknownSettingKey(key, sorted(levels))
    resolved = rules.resolve_chain(levels[key], template)
    return {
        "key": key,
        "page": page_name,
        "value": resolved["value"],
        "source": resolved["source"],
        "chain": resolved["chain"],
        "explanation": rules.explain_text(key, page_name, resolved),
    }


def can_apply(tid: str, role: str, action: str, key: str, value: Any) -> rules.Decision:
    facts = queries.can_apply_facts(get_driver(), tid, role, action, key)
    return rules.evaluate_can_apply(facts, action, key.strip().lower(), value)


def impact_of_change(tid: str, template_name: str, key: str | None = None) -> dict[str, Any]:
    """What would change on this tenant's pages if it switched to another template."""
    driver = get_driver()
    tenant = queries.tenant_facts(driver, tid)
    if tenant is None:
        raise TenantNotFound(tid)
    candidate = queries.template_values(driver, template_name)
    if candidate is None:
        raise TemplateNotFound(template_name, queries.template_names(driver))
    current = queries.template_values(driver, tenant["template"]) if tenant["template"] else {}
    defaults = {k: d["default"] for k, d in queries.setting_keys(driver).items()}
    if key:
        if key not in defaults:
            raise UnknownSettingKey(key, sorted(defaults))
        defaults = {key: defaults[key]}
    changes = rules.template_switch_impact(queries.overrides_by_page(driver, tid), current or {}, candidate, defaults)
    return {
        "current_template": tenant["template"],
        "candidate_template": template_name.strip().lower(),
        "changes": changes,
        "summary": f"{len(changes)} effective value(s) would change.",
    }


def resolve_entity(tid: str, text: str) -> list[dict[str, Any]]:
    """Map loose wording to graph entities: setting keys (with aliases), pages, templates."""
    driver = get_driver()
    query = text.strip().lower()
    tokens = set(re.findall(r"[a-z0-9_]+", query))
    found: list[dict[str, Any]] = []

    for name, d in queries.setting_keys(driver).items():
        names = [name, name.replace("_", " "), *d["aliases"]]
        if any(n in query for n in names) or tokens & {name}:
            found.append({"type": "setting", "name": name, "score": 1.0})
    for page in queries.page_names(driver, tid):
        score = 1.0 if page.lower() in tokens else SequenceMatcher(None, query, page.lower()).ratio()
        if score >= 0.6:
            found.append({"type": "page", "name": page, "score": round(score, 2)})
    for template in queries.template_names(driver):
        if template in query or template.replace("_", " ") in query:
            found.append({"type": "template", "name": template, "score": 1.0})
    return sorted(found, key=lambda f: -f["score"])


def related(tid: str, entity_type: str, name: str, depth: int = 1) -> list[dict[str, Any]]:
    label = RELATABLE.get(entity_type.strip().lower())
    if label is None:
        raise UnknownSettingKey(entity_type, sorted(RELATABLE))  # "unknown option" with the valid choices
    key = f"{tid}:{name.strip().lower()}" if label == "Page" else name.strip().lower()
    return queries.find_related(get_driver(), tid, label, key, max(1, min(depth, 2)))


def ontology_rule_ids() -> list[str]:
    return [r.id for r in load_ontology().rules]
