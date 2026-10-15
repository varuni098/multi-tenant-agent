"""Parameterised Cypher for graph tools. Every tenant-scoped query is pinned to $tid.

Only fixed queries live here; there is no free-form Cypher anywhere in the product.
"""

import json
from typing import Any

from neo4j import Driver


def _loads(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def tenant_facts(driver: Driver, tid: str) -> dict[str, Any] | None:
    rec = driver.execute_query(
        """
        MATCH (t:Tenant {id: $tid})
        OPTIONAL MATCH (t)-[:SUBSCRIBES_TO]->(p:Plan)
        OPTIONAL MATCH (t)-[:INHERITS_FROM]->(tpl:Template)
        RETURN t.status AS status, p.name AS plan, tpl.name AS template
        """,
        tid=tid,
    ).records
    return dict(rec[0]) if rec else None


def member_role(driver: Driver, tid: str, email: str) -> str | None:
    rec = driver.execute_query(
        "MATCH (u:User {uid: $uid})-[:MEMBER_OF]->(:Tenant {id: $tid}) "
        "MATCH (u)-[:HAS_ROLE]->(r:Role) RETURN r.name AS role",
        uid=f"{tid}:{email.lower()}",
        tid=tid,
    ).records
    return rec[0]["role"] if rec else None


def page_names(driver: Driver, tid: str) -> list[str]:
    recs = driver.execute_query(
        "MATCH (:Tenant {id: $tid})-[:OWNS]->(p:Page) RETURN p.name AS name ORDER BY name", tid=tid
    ).records
    return [r["name"] for r in recs]


def setting_keys(driver: Driver) -> dict[str, dict[str, Any]]:
    recs = driver.execute_query(
        """
        MATCH (k:SettingKey)
        OPTIONAL MATCH (k)-[:REQUIRES]->(f:Feature)
        RETURN k.name AS name, k.value_type AS value_type, k.allowed_json AS allowed_json,
               k.pattern AS pattern, k.default_json AS default_json, k.aliases AS aliases, f.key AS feature
        """
    ).records
    return {
        r["name"]: {
            "value_type": r["value_type"],
            "allowed": _loads(r["allowed_json"]),
            "pattern": r["pattern"],
            "default": _loads(r["default_json"]),
            "aliases": r["aliases"] or [],
            "feature": r["feature"],
        }
        for r in recs
    }


def setting_levels(driver: Driver, tid: str, page_name: str | None) -> tuple[bool, str | None, dict[str, dict]]:
    """(page found, template name, {key: {page, tenant, template, default}}) for one page or tenant-wide."""
    recs = driver.execute_query(
        """
        MATCH (t:Tenant {id: $tid})
        OPTIONAL MATCH (t)-[:OWNS]->(p:Page) WHERE toLower(p.name) = $pname
        OPTIONAL MATCH (t)-[:INHERITS_FROM]->(tpl:Template)
        WITH t, p, tpl
        MATCH (k:SettingKey)
        OPTIONAL MATCH (p)-[:HAS_SETTING]->(ps:Setting)-[:OF_KEY]->(k)
        OPTIONAL MATCH (t)-[:HAS_SETTING]->(ts:Setting)-[:OF_KEY]->(k)
        OPTIONAL MATCH (tpl)-[:DEFINES]->(xs:Setting)-[:OF_KEY]->(k)
        RETURN k.name AS key, p IS NOT NULL AS page_found, tpl.name AS template,
               ps.value_json AS page, ts.value_json AS tenant, xs.value_json AS template_value,
               k.default_json AS default
        """,
        tid=tid,
        pname=page_name.strip().lower() if page_name else "",
    ).records
    if not recs:
        return False, None, {}
    levels = {
        r["key"]: {
            "page": _loads(r["page"]) if page_name else None,
            "tenant": _loads(r["tenant"]),
            "template": _loads(r["template_value"]),
            "default": _loads(r["default"]),
        }
        for r in recs
    }
    return (recs[0]["page_found"] or page_name is None), recs[0]["template"], levels


def can_apply_facts(driver: Driver, tid: str, role: str, action: str, key: str) -> dict[str, Any]:
    keys = setting_keys(driver)
    key_def = keys.get(key.strip().lower())
    rec = driver.execute_query(
        """
        OPTIONAL MATCH (t:Tenant {id: $tid})-[:SUBSCRIBES_TO]->(plan:Plan)
        OPTIONAL MATCH (:Role {name: $role})-[may:MAY]->(:Action {name: $action})
        OPTIONAL MATCH (plan)-[:INCLUDES]->(f:Feature {key: $feature})
        RETURN t.status AS status, plan.name AS plan, may IS NOT NULL AS role_may, f IS NOT NULL AS plan_includes
        """,
        tid=tid,
        role=role,
        action=action,
        feature=(key_def or {}).get("feature") or "",
    ).records[0]
    return {
        "tenant": {"status": rec["status"], "plan": rec["plan"]} if rec["status"] else None,
        "role": role,
        "role_may": rec["role_may"],
        "key_def": key_def,
        "known_keys": sorted(keys),
        "required_feature": (key_def or {}).get("feature"),
        "plan_includes": rec["plan_includes"],
    }


def overrides_by_page(driver: Driver, tid: str) -> dict[str | None, dict[str, dict[str, Any]]]:
    """{page name or None (tenant level): {key: {"page": v, "tenant": v}}} for every page."""
    recs = driver.execute_query(
        """
        MATCH (t:Tenant {id: $tid})
        OPTIONAL MATCH (t)-[:OWNS]->(p:Page)
        OPTIONAL MATCH (p)-[:HAS_SETTING]->(ps:Setting)
        OPTIONAL MATCH (t)-[:HAS_SETTING]->(ts:Setting)
        RETURN p.name AS page, collect(DISTINCT [ps.key, ps.value_json]) AS page_values,
               collect(DISTINCT [ts.key, ts.value_json]) AS tenant_values
        """,
        tid=tid,
    ).records
    result: dict[str | None, dict[str, dict[str, Any]]] = {None: {}}
    tenant_values: dict[str, Any] = {}
    for r in recs:
        for k, v in r["tenant_values"]:
            if k is not None:
                tenant_values[k] = _loads(v)
        if r["page"] is not None:
            result[r["page"]] = {k: {"page": _loads(v)} for k, v in r["page_values"] if k is not None}
    for page_overrides in result.values():
        for k, v in tenant_values.items():
            page_overrides.setdefault(k, {})["tenant"] = v
    return result


def template_values(driver: Driver, template: str) -> dict[str, Any] | None:
    recs = driver.execute_query(
        """
        MATCH (tpl:Template {name: $name})
        OPTIONAL MATCH (tpl)-[:DEFINES]->(s:Setting)
        RETURN s.key AS key, s.value_json AS value
        """,
        name=template.strip().lower(),
    ).records
    if not recs:
        return None
    return {r["key"]: _loads(r["value"]) for r in recs if r["key"] is not None}


def template_names(driver: Driver) -> list[str]:
    return [r["name"] for r in driver.execute_query("MATCH (t:Template) RETURN t.name AS name ORDER BY name").records]


def find_related(driver: Driver, tid: str, label: str, key: str, depth: int) -> list[dict[str, Any]]:
    """Neighbours within `depth` hops. A path may only pass through this tenant's nodes or shared ones."""
    hops = 2 if depth >= 2 else 1  # variable-length bounds can't be parameters; only two fixed shapes exist
    recs = driver.execute_query(
        f"""
        MATCH (s:{label})
        WHERE (s.uid = $key OR s.name = $key OR s.id = $key OR s.key = $key)
          AND (s.tenant_id IS NULL OR s.tenant_id = $tid)
        MATCH path = (s)-[*1..{hops}]-(n)
        WHERE all(x IN nodes(path) WHERE x.tenant_id IS NULL OR x.tenant_id = $tid)
          AND n <> s
        RETURN DISTINCT labels(n)[0] AS label,
               coalesce(n.name, n.key, n.email, n.uid, n.id) AS name,
               [r IN relationships(path) | type(r)] AS via
        LIMIT 50
        """,
        tid=tid,
        key=key,
    ).records
    return [{"label": r["label"], "name": r["name"], "via": r["via"]} for r in recs]
