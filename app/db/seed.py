"""Idempotent seed data: `python -m app.db.seed`.

Reference data (plans, features, setting keys, templates) normally comes from
migration 0002; it is ensured here too so test databases built with
`create_all` match. Demo tenants are for local development only.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Feature,
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
from app.db.session import session_scope

FEATURES = {"custom_branding": "Custom brand colour"}
PLANS = {"free": [], "pro": ["custom_branding"]}
SETTING_KEYS: list[dict[str, Any]] = [
    {"name": "theme", "value_type": "enum", "allowed_values": ["light", "dark"], "default_value": "light"},
    {"name": "language", "value_type": "enum", "allowed_values": ["en", "es", "fr", "de"], "default_value": "en"},
    {
        "name": "layout",
        "value_type": "enum",
        "allowed_values": ["comfortable", "compact"],
        "default_value": "comfortable",
    },
    {
        "name": "brand_color",
        "value_type": "string",
        "pattern": r"^#[0-9a-fA-F]{6}$",
        "default_value": "#2563eb",
        "feature": "custom_branding",
    },
    {"name": "show_tips", "value_type": "bool", "default_value": True},
]
TEMPLATES = {
    "standard": {"theme": "light", "layout": "comfortable"},
    "dark_first": {"theme": "dark", "layout": "compact"},
}

TENANT_A = "a1b2c3d4-e5f6-7890-abcd-ef0123456789"
TENANT_B = "b2c3d4e5-f6a7-8901-bcde-f01234567890"
DEMO_TENANTS: list[dict[str, Any]] = [
    {
        "id": TENANT_A,
        "name": "Acme",
        "plan": "free",
        "template": "standard",
        "pages": {"login": {"theme": "light"}, "dashboard": {"theme": "dark"}, "settings": {}},
        "users": {"admin@acme.test": "admin", "editor@acme.test": "editor"},
    },
    {
        "id": TENANT_B,
        "name": "Globex",
        "plan": "pro",
        "template": "standard",
        "pages": {"login": {"theme": "dark"}},
        "users": {"admin@globex.test": "admin"},
    },
]


def _get_or_add(session: Session, model: type, lookup: dict[str, Any], **values: Any) -> Any:
    obj = session.scalars(select(model).filter_by(**lookup)).first()
    if obj is None:
        obj = model(**lookup, **values)
        session.add(obj)
        session.flush()
    return obj


def ensure_reference_data(session: Session) -> None:
    features = {k: _get_or_add(session, Feature, {"key": k}, description=d) for k, d in FEATURES.items()}
    for plan_name, feature_keys in PLANS.items():
        plan = _get_or_add(session, Plan, {"name": plan_name})
        for fk in feature_keys:
            _get_or_add(session, PlanFeature, {"plan_id": plan.id, "feature_id": features[fk].id})
    for spec in SETTING_KEYS:
        spec = dict(spec)
        feature = spec.pop("feature", None)
        name = spec.pop("name")
        _get_or_add(
            session, SettingKey, {"name": name}, required_feature_id=features[feature].id if feature else None, **spec
        )
    for template_name, values in TEMPLATES.items():
        template = _get_or_add(session, Template, {"name": template_name})
        for key, value in values.items():
            _get_or_add(session, TemplateSetting, {"template_id": template.id, "setting_key": key}, value=value)


def seed_demo_tenants(session: Session) -> int:
    added = 0
    for spec in DEMO_TENANTS:
        tenant = session.get(Tenant, spec["id"])
        if tenant is None:
            plan = session.scalars(select(Plan).where(Plan.name == spec["plan"])).one()
            template = session.scalars(select(Template).where(Template.name == spec["template"])).one()
            tenant = Tenant(id=spec["id"], name=spec["name"], plan_id=plan.id, template_id=template.id)
            session.add(tenant)
            session.flush()
            added += 1
        for page_name, page_settings in spec["pages"].items():
            page = session.scalars(select(Page).where(Page.tenant_id == tenant.id, Page.name == page_name)).first()
            if page is None:
                page = Page(tenant_id=tenant.id, name=page_name)
                session.add(page)
                session.flush()
                added += 1
                for key, value in page_settings.items():
                    session.add(Setting(tenant_id=tenant.id, page_id=page.id, setting_key=key, value=value))
        for email, role in spec["users"].items():
            if not session.scalars(select(User).where(User.tenant_id == tenant.id, User.email == email)).first():
                session.add(User(tenant_id=tenant.id, email=email, role=role))
                added += 1
    return added


def seed() -> int:
    with session_scope() as session:
        ensure_reference_data(session)
        return seed_demo_tenants(session)


if __name__ == "__main__":
    print(f"Seeded {seed()} new row(s).")
