"""tool_calls: server-side log of MCP tool calls

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, index=True),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("tool", sa.String(50), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tool_calls")
