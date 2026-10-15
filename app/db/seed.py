"""Idempotent demo data for local development: `python -m app.db.seed`."""

from sqlalchemy import select

from app.db.models import TenantPageEvent
from app.db.session import session_scope

DEMO_ROWS = [
    ("a1b2c3d4-e5f6-7890-abcd-ef0123456789", "login", "light"),
    ("a1b2c3d4-e5f6-7890-abcd-ef0123456789", "dashboard", "dark"),
    ("b2c3d4e5-f6a7-8901-bcde-f01234567890", "login", "dark"),
]


def seed() -> int:
    added = 0
    with session_scope() as session:
        for tenant_uuid, page_name, theme in DEMO_ROWS:
            exists = session.scalars(
                select(TenantPageEvent).where(
                    TenantPageEvent.tenant_uuid == tenant_uuid,
                    TenantPageEvent.page_name == page_name,
                )
            ).first()
            if not exists:
                session.add(TenantPageEvent(tenant_uuid=tenant_uuid, page_name=page_name, theme=theme))
                added += 1
    return added


if __name__ == "__main__":
    print(f"Seeded {seed()} new row(s).")
