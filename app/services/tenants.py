from sqlalchemy import select

from app.db.models import Tenant, User
from app.db.session import session_scope


def is_tenant_active(tenant_id: str) -> bool:
    with session_scope() as session:
        tenant = session.get(Tenant, tenant_id)
        return tenant is not None and tenant.status == "active"


def get_user_role(tenant_id: str, email: str) -> str | None:
    with session_scope() as session:
        user = session.scalars(select(User).where(User.tenant_id == tenant_id, User.email == email.lower())).first()
        return user.role if user else None


def find_user_tenants(email: str) -> list[tuple[str, str]]:
    """(tenant_id, role) for every tenant the user belongs to."""
    with session_scope() as session:
        rows = session.scalars(select(User).where(User.email == email.lower()))
        return [(u.tenant_id, u.role) for u in rows]
