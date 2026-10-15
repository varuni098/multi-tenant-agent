from sqlalchemy import func, select

from app.db.models import TenantPageEvent
from app.db.session import session_scope


def _page_query(tenant_uuid: str, page_name: str):
    return select(TenantPageEvent).where(
        TenantPageEvent.tenant_uuid == tenant_uuid,
        func.lower(TenantPageEvent.page_name) == page_name.lower(),
    )


def get_tenant_page_theme(tenant_uuid: str, page_name: str) -> str:
    """Gets current theme for a specific tenant UUID and page name."""
    with session_scope() as session:
        row = session.scalars(_page_query(tenant_uuid, page_name)).first()
        if row:
            return (
                f"Tenant {tenant_uuid} page '{page_name}' (Event ID: {row.event_id}) is currently in {row.theme} mode."
            )
        return f"No page entry found for page '{page_name}' under tenant {tenant_uuid}."


def toggle_tenant_page_theme(tenant_uuid: str, page_name: str) -> str:
    """Toggles theme state between light and dark for a specific tenant page event."""
    with session_scope() as session:
        # Row lock so concurrent toggles can't both read the same old value
        row = session.scalars(_page_query(tenant_uuid, page_name).with_for_update()).first()
        if not row:
            return f"Error: Could not find page '{page_name}' for tenant {tenant_uuid}."

        current_theme = row.theme
        row.theme = "dark" if current_theme == "light" else "light"
        return (
            f"Success: Page '{page_name}' (Event ID: {row.event_id}) for tenant {tenant_uuid} "
            f"switched from {current_theme} to {row.theme} mode."
        )


def get_all_tenant_pages() -> list[dict]:
    """Fetch all tenant page records for validation."""
    with session_scope() as session:
        rows = session.scalars(select(TenantPageEvent)).all()
        return [{"tenant_uuid": r.tenant_uuid, "page_name": r.page_name, "theme": r.theme} for r in rows]


def ping() -> None:
    """Raise if the database is unreachable."""
    with session_scope() as session:
        session.execute(select(1))
