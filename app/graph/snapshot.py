"""The expected knowledge graph, built from MySQL (the system of record).

The sync worker and the reconciler both project with these functions, so the graph is always
"what MySQL says", whichever path wrote it.
"""

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    Feature,
    OutboxEvent,
    Page,
    Plan,
    PlanFeature,
    Setting,
    SettingKey,
    Template,
    TemplateSetting,
    Tenant,
    User,
)
from app.graph.model import Graph
from app.graph.ontology import load_ontology
from app.harness.context import ROLE_RANK
from app.harness.policy import TOOL_POLICIES, role_allows


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def page_uid(tenant_id: str, page_name: str) -> str:
    return f"{tenant_id}:{page_name}"


def setting_uid(tenant_id: str, page_name: str | None, key: str) -> str:
    return f"{tenant_id}:{page_name or '*'}:{key}"


def reference_graph(session: Session) -> Graph:
    """Shared nodes: roles, actions, plans, features, setting keys, templates and their values."""
    ontology = load_ontology()
    g = Graph()
    for role, rank in ROLE_RANK.items():
        g.add_node("Role", {"name": role, "rank": rank})
    for action in TOOL_POLICIES:
        g.add_node("Action", {"name": action})
        for role in ROLE_RANK:
            if role_allows(role, action):  # type: ignore[arg-type]
                g.add_rel("MAY", ("Role", role), ("Action", action))

    features = {f.id: f.key for f in session.scalars(select(Feature))}
    for f in session.scalars(select(Feature)):
        g.add_node("Feature", {"key": f.key, "description": f.description})
    plans = {p.id: p.name for p in session.scalars(select(Plan))}
    for name in plans.values():
        g.add_node("Plan", {"name": name})
    for pf in session.scalars(select(PlanFeature)):
        g.add_rel("INCLUDES", ("Plan", plans[pf.plan_id]), ("Feature", features[pf.feature_id]))

    for k in session.scalars(select(SettingKey)):
        g.add_node(
            "SettingKey",
            {
                "name": k.name,
                "value_type": k.value_type,
                "allowed_json": dumps(k.allowed_values) if k.allowed_values is not None else None,
                "pattern": k.pattern,
                "default_json": dumps(k.default_value),
                "description": k.description,
                "aliases": list(ontology.aliases.get(k.name, ())),
            },
        )
        if k.required_feature_id is not None:
            g.add_rel("REQUIRES", ("SettingKey", k.name), ("Feature", features[k.required_feature_id]))

    templates = {t.id: t.name for t in session.scalars(select(Template))}
    for name in templates.values():
        g.add_node("Template", {"name": name})
    for ts in session.scalars(select(TemplateSetting)):
        tpl = templates[ts.template_id]
        uid = f"tpl:{tpl}:{ts.setting_key}"
        g.add_node("Setting", {"uid": uid, "key": ts.setting_key, "value_json": dumps(ts.value), "scope": "template"})
        g.add_rel("DEFINES", ("Template", tpl), ("Setting", uid))
        g.add_rel("OF_KEY", ("Setting", uid), ("SettingKey", ts.setting_key))
    return g


def tenant_graph(session: Session, tenant_id: str, max_events: int = 50) -> Graph | None:
    """One tenant's nodes and their edges to reference nodes; None if the tenant no longer exists."""
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        return None
    g = Graph()
    t = g.add_node(
        "Tenant",
        {
            "id": tenant.id,
            "tenant_id": tenant.id,
            "name": tenant.name,
            "status": tenant.status,
            "region": tenant.region,
        },
    )
    g.add_rel("SUBSCRIBES_TO", t, ("Plan", tenant.plan.name))
    if tenant.template is not None:
        g.add_rel("INHERITS_FROM", t, ("Template", tenant.template.name))

    for u in session.scalars(select(User).where(User.tenant_id == tenant_id)):
        ref = g.add_node("User", {"uid": f"{tenant_id}:{u.email}", "tenant_id": tenant_id, "email": u.email})
        g.add_rel("MEMBER_OF", ref, t)
        g.add_rel("HAS_ROLE", ref, ("Role", u.role))

    pages = {p.id: p.name for p in session.scalars(select(Page).where(Page.tenant_id == tenant_id))}
    for name in pages.values():
        g.add_rel(
            "OWNS", t, g.add_node("Page", {"uid": page_uid(tenant_id, name), "tenant_id": tenant_id, "name": name})
        )

    for s in session.scalars(select(Setting).where(Setting.tenant_id == tenant_id)):
        page_name = pages.get(s.page_id) if s.page_id is not None else None
        uid = setting_uid(tenant_id, page_name, s.setting_key)
        ref = g.add_node(
            "Setting",
            {
                "uid": uid,
                "tenant_id": tenant_id,
                "key": s.setting_key,
                "value_json": dumps(s.value),
                "scope": "page" if page_name else "tenant",
            },
        )
        owner = ("Page", page_uid(tenant_id, page_name)) if page_name else t
        g.add_rel("HAS_SETTING", owner, ref)
        g.add_rel("OF_KEY", ref, ("SettingKey", s.setting_key))

    page_names = set(pages.values())
    recent = session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.id.desc()).limit(max_events)
    )
    for a in recent:
        ref = g.add_node(
            "ChangeEvent",
            {
                "uid": f"audit:{a.id}",
                "tenant_id": tenant_id,
                "action": a.action,
                "actor": a.actor,
                "key": a.setting_key,
                "page_name": a.page_name,
                "before_json": dumps(a.before) if a.before is not None else None,
                "after_json": dumps(a.after) if a.after is not None else None,
                "at": a.created_at.isoformat() if a.created_at else None,
            },
        )
        if a.page_name in page_names:
            g.add_rel("CHANGED", ref, ("Page", page_uid(tenant_id, a.page_name)))
        else:
            g.add_rel("CHANGED", ref, t)
    return g


def tenant_ids(session: Session) -> list[str]:
    return list(session.scalars(select(Tenant.id).order_by(Tenant.id)))


def outbox_watermark(session: Session) -> int:
    return session.scalar(select(func.max(OutboxEvent.id))) or 0


def full_graph(session: Session, max_events: int = 50) -> Graph:
    g = reference_graph(session)
    for tid in tenant_ids(session):
        tg = tenant_graph(session, tid, max_events)
        if tg is not None:
            g.merge(tg)
    return g
