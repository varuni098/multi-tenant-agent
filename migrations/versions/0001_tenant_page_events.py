"""tenant_page_events baseline

Revision ID: 0001
Revises:
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Baseline: databases created before Alembic already have this table, so leave them as-is
    if sa.inspect(op.get_bind()).has_table("tenant_page_events"):
        return
    op.create_table(
        "tenant_page_events",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_uuid", sa.String(36), nullable=False),
        sa.Column("page_name", sa.String(100), nullable=False),
        sa.Column("theme", sa.Enum("light", "dark", name="theme"), nullable=False, server_default="light"),
        sa.UniqueConstraint("tenant_uuid", "page_name", name="uq_tenant_page"),
    )


def downgrade() -> None:
    op.drop_table("tenant_page_events")
