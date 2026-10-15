"""tenants, pages, settings, templates, plans, audit log and outbox

Moves tenant_page_events rows into tenants/pages/settings and replaces the old
table with a read-only compatibility view (MySQL).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Frozen copy of the reference data (migrations must not import app code)
PLANS = ["free", "pro"]
FEATURES = [("custom_branding", "Custom brand colour")]
PLAN_FEATURES = [("pro", "custom_branding")]
SETTING_KEYS = [
    # name, type, allowed, pattern, default, required feature, description
    ("theme", "enum", ["light", "dark"], None, "light", None, "Colour theme"),
    ("language", "enum", ["en", "es", "fr", "de"], None, "en", None, "UI language"),
    ("layout", "enum", ["comfortable", "compact"], None, "comfortable", None, "Content density"),
    ("brand_color", "string", None, r"^#[0-9a-fA-F]{6}$", "#2563eb", "custom_branding", "Brand colour (hex)"),
    ("show_tips", "bool", None, None, True, None, "Show onboarding tips"),
]
TEMPLATES = {
    "standard": {"theme": "light", "layout": "comfortable"},
    "dark_first": {"theme": "dark", "layout": "compact"},
}

COMPAT_VIEW = """
CREATE VIEW tenant_page_events AS
SELECT s.id AS event_id, p.tenant_id AS tenant_uuid, p.name AS page_name,
       JSON_UNQUOTE(s.value) AS theme
FROM settings s JOIN pages p ON p.id = s.page_id
WHERE s.setting_key = 'theme'
"""


def upgrade() -> None:
    bind = op.get_bind()

    plans = op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
    )
    features = op.create_table(
        "features",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(50), nullable=False, unique=True),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
    )
    plan_features = op.create_table(
        "plan_features",
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id"), primary_key=True),
        sa.Column("feature_id", sa.Integer(), sa.ForeignKey("features.id"), primary_key=True),
    )
    setting_keys = op.create_table(
        "setting_keys",
        sa.Column("name", sa.String(50), primary_key=True),
        sa.Column("value_type", sa.Enum("enum", "string", "bool", name="value_type"), nullable=False),
        sa.Column("allowed_values", sa.JSON(), nullable=True),
        sa.Column("pattern", sa.String(255), nullable=True),
        sa.Column("default_value", sa.JSON(), nullable=False),
        sa.Column("required_feature_id", sa.Integer(), sa.ForeignKey("features.id"), nullable=True),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
    )
    templates = op.create_table(
        "templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
    )
    template_settings = op.create_table(
        "template_settings",
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("templates.id"), primary_key=True),
        sa.Column("setting_key", sa.String(50), sa.ForeignKey("setting_keys.name"), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
    )
    tenants = op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("templates.id"), nullable=True),
        sa.Column("status", sa.Enum("active", "suspended", name="tenant_status"), nullable=False),
        sa.Column("region", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", sa.Enum("viewer", "editor", "admin", name="user_role"), nullable=False),
        sa.UniqueConstraint("tenant_id", "email", name="uq_tenant_user_email"),
    )
    pages = op.create_table(
        "pages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_tenant_page_name"),
    )
    settings = op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("page_id", sa.Integer(), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=True),
        sa.Column("setting_key", sa.String(50), sa.ForeignKey("setting_keys.name"), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "page_id", "setting_key", name="uq_tenant_page_setting"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, index=True),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("page_name", sa.String(100), nullable=True),
        sa.Column("setting_key", sa.String(50), nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("reverts_id", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("aggregate_type", sa.String(50), nullable=False),
        sa.Column("aggregate_id", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True, index=True),
    )

    # --- Reference data ---
    op.bulk_insert(plans, [{"id": i, "name": n} for i, n in enumerate(PLANS, start=1)])
    op.bulk_insert(features, [{"id": i, "key": k, "description": d} for i, (k, d) in enumerate(FEATURES, start=1)])
    plan_ids = {n: i for i, n in enumerate(PLANS, start=1)}
    feature_ids = {k: i for i, (k, _) in enumerate(FEATURES, start=1)}
    op.bulk_insert(plan_features, [{"plan_id": plan_ids[p], "feature_id": feature_ids[f]} for p, f in PLAN_FEATURES])
    op.bulk_insert(
        setting_keys,
        [
            {
                "name": name,
                "value_type": vtype,
                "allowed_values": allowed,
                "pattern": pattern,
                "default_value": default,
                "required_feature_id": feature_ids[feature] if feature else None,
                "description": desc,
            }
            for name, vtype, allowed, pattern, default, feature, desc in SETTING_KEYS
        ],
    )
    op.bulk_insert(templates, [{"id": i, "name": n} for i, n in enumerate(TEMPLATES, start=1)])
    op.bulk_insert(
        template_settings,
        [
            {"template_id": i, "setting_key": key, "value": value}
            for i, values in enumerate(TEMPLATES.values(), start=1)
            for key, value in values.items()
        ],
    )

    # --- Move existing tenant_page_events data, then swap the table for a view ---
    if sa.inspect(bind).has_table("tenant_page_events"):
        all_rows = bind.execute(
            sa.text("SELECT tenant_uuid, page_name, theme FROM tenant_page_events ORDER BY event_id")
        ).all()
        # Page names are unique per tenant case-insensitively; keep the first of any duplicates
        seen: set[tuple[str, str]] = set()
        rows = []
        for r in all_rows:
            if (r.tenant_uuid, r.page_name.lower()) not in seen:
                seen.add((r.tenant_uuid, r.page_name.lower()))
                rows.append(r)
        tenant_ids = sorted({r.tenant_uuid for r in rows})
        op.bulk_insert(
            tenants,
            [
                {"id": t, "name": f"Tenant {t[:8]}", "plan_id": plan_ids["free"], "template_id": 1, "status": "active"}
                for t in tenant_ids
            ],
        )
        page_rows = [{"id": i, "tenant_id": r.tenant_uuid, "name": r.page_name} for i, r in enumerate(rows, start=1)]
        op.bulk_insert(pages, page_rows)
        op.bulk_insert(
            settings,
            [
                {"tenant_id": r.tenant_uuid, "page_id": i, "setting_key": "theme", "value": r.theme, "version": 1}
                for i, r in enumerate(rows, start=1)
            ],
        )
        op.drop_table("tenant_page_events")

    if bind.dialect.name == "mysql":
        op.execute(COMPAT_VIEW)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("DROP VIEW IF EXISTS tenant_page_events")

    old = op.create_table(
        "tenant_page_events",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_uuid", sa.String(36), nullable=False),
        sa.Column("page_name", sa.String(100), nullable=False),
        sa.Column("theme", sa.Enum("light", "dark", name="theme"), nullable=False, server_default="light"),
        sa.UniqueConstraint("tenant_uuid", "page_name", name="uq_tenant_page"),
    )
    # Page-level theme overrides go back; inherited themes and other settings are lost
    rows = bind.execute(
        sa.text(
            "SELECT p.tenant_id, p.name, s.value FROM settings s JOIN pages p ON p.id = s.page_id "
            "WHERE s.setting_key = 'theme'"
        )
    ).all()
    # Raw SQL returns JSON as text, e.g. '"dark"'
    op.bulk_insert(
        old,
        [{"tenant_uuid": t, "page_name": n, "theme": json.loads(v) if isinstance(v, str) else v} for t, n, v in rows],
    )

    for table in [
        "outbox",
        "audit_log",
        "settings",
        "pages",
        "users",
        "tenants",
        "template_settings",
        "templates",
        "setting_keys",
        "plan_features",
        "features",
        "plans",
    ]:
        op.drop_table(table)
