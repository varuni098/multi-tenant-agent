from app.db.models import Tenant
from app.db.session import session_scope


def is_tenant_active(tenant_id: str) -> bool:
    with session_scope() as session:
        tenant = session.get(Tenant, tenant_id)
        return tenant is not None and tenant.status == "active"
