from sqlalchemy import Enum, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TenantPageEvent(Base):
    __tablename__ = "tenant_page_events"
    __table_args__ = (UniqueConstraint("tenant_uuid", "page_name", name="uq_tenant_page"),)

    event_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_uuid: Mapped[str] = mapped_column(String(36))
    page_name: Mapped[str] = mapped_column(String(100))
    theme: Mapped[str] = mapped_column(Enum("light", "dark", name="theme"), default="light")
