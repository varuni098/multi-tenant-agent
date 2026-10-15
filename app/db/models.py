from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --- Reference data: plans, features, setting definitions, templates ---


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)


class Feature(Base):
    __tablename__ = "features"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(50), unique=True)
    description: Mapped[str] = mapped_column(String(255), default="")


class PlanFeature(Base):
    __tablename__ = "plan_features"

    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), primary_key=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"), primary_key=True)


class SettingKey(Base):
    """Definition of a configurable setting: type, allowed values, default, required feature."""

    __tablename__ = "setting_keys"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    value_type: Mapped[str] = mapped_column(Enum("enum", "string", "bool", name="value_type"))
    allowed_values: Mapped[Any] = mapped_column(JSON, nullable=True)
    pattern: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_value: Mapped[Any] = mapped_column(JSON)
    required_feature_id: Mapped[int | None] = mapped_column(ForeignKey("features.id"), nullable=True)
    description: Mapped[str] = mapped_column(String(255), default="")

    required_feature: Mapped[Feature | None] = relationship()


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)


class TemplateSetting(Base):
    __tablename__ = "template_settings"

    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"), primary_key=True)
    setting_key: Mapped[str] = mapped_column(ForeignKey("setting_keys.name"), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


# --- Tenant data ---


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # tenant UUID
    name: Mapped[str] = mapped_column(String(100))
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("templates.id"), nullable=True)
    status: Mapped[str] = mapped_column(Enum("active", "suspended", name="tenant_status"), default="active")
    region: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    plan: Mapped[Plan] = relationship()
    template: Mapped[Template | None] = relationship()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_tenant_user_email"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    email: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(Enum("viewer", "editor", "admin", name="user_role"), default="viewer")


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tenant_page_name"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Setting(Base):
    """A setting override: page-level when page_id is set, tenant-wide default when page_id is NULL."""

    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("tenant_id", "page_id", "setting_key", name="uq_tenant_page_setting"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    page_id: Mapped[int | None] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), nullable=True)
    setting_key: Mapped[str] = mapped_column(ForeignKey("setting_keys.name"))
    value: Mapped[Any] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Optimistic locking: concurrent writers of the same row get StaleDataError
    __mapper_args__ = {"version_id_col": version}


# --- Audit trail and outbox (written in the same transaction as each change) ---


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)  # no FK: audit outlives deleted data
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(50))
    page_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    setting_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    before: Mapped[Any] = mapped_column(JSON, nullable=True)  # the override before; NULL = inherited
    after: Mapped[Any] = mapped_column(JSON, nullable=True)
    reverts_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # set on "undo" entries
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class OutboxEvent(Base):
    """Events for downstream consumers (graph sync, live UI); a publisher marks published_at."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    aggregate_type: Mapped[str] = mapped_column(String(50))
    aggregate_id: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[Any] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class ToolCall(Base):
    """Server-side log of every MCP tool call, including denied and failed ones."""

    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str] = mapped_column(String(20))  # agent | external
    tool: Mapped[str] = mapped_column(String(50))
    args: Mapped[Any] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String(20))  # ok | error | denied
    duration_ms: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
