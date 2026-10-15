"""Admin operations. Destructive and bulk ones go through the harness confirmation gate."""

from typing import Any

from sqlalchemy import delete, select

from app.db.models import Page, Setting
from app.db.session import session_scope
from app.services import events
from app.services.errors import PageAlreadyExists, PageNotFound
from app.services.schemas import ChangeResult, PageInfo
from app.services.settings import (
    apply_setting,
    check_entitled,
    coerce_value,
    get_key,
    get_page,
    get_template,
    get_tenant,
    page_names,
    resolve_all,
)


def create_page(
    tenant_id: str, name: str, theme: str | None = None, *, actor: str = "system", request_id: str | None = None
) -> PageInfo:
    with session_scope() as session:
        tenant = get_tenant(session, tenant_id)
        name = name.strip().lower()
        if name in (n.lower() for n in page_names(session, tenant.id)):
            raise PageAlreadyExists(name)
        page = Page(tenant_id=tenant.id, name=name)
        session.add(page)
        session.flush()
        events.record(
            session,
            tenant_id=tenant.id,
            actor=actor,
            action="create_page",
            event_type="page.created",
            page_name=name,
            request_id=request_id,
        )
        if theme is not None:
            key_def = get_key(session, "theme")
            apply_setting(
                session, tenant, page, key_def, coerce_value(key_def, theme), actor=actor, request_id=request_id
            )
        return PageInfo(name=name, theme=resolve_all(session, tenant, page)["theme"].value)


def delete_page(tenant_id: str, name: str, *, actor: str = "system", request_id: str | None = None) -> None:
    with session_scope() as session:
        tenant = get_tenant(session, tenant_id)
        page = get_page(session, tenant, name)
        overrides = {s.setting_key: s.value for s in session.scalars(select(Setting).where(Setting.page_id == page.id))}
        session.execute(delete(Setting).where(Setting.page_id == page.id))
        session.delete(page)
        events.record(
            session,
            tenant_id=tenant.id,
            actor=actor,
            action="delete_page",
            event_type="page.deleted",
            page_name=page.name,
            before=overrides,
            request_id=request_id,
        )


def bulk_set_setting(
    tenant_id: str,
    page_names_: list[str],
    key: str,
    value: Any,
    *,
    actor: str = "system",
    request_id: str | None = None,
) -> list[ChangeResult]:
    """All-or-nothing: every page must exist and the value must be valid before anything changes."""
    with session_scope() as session:
        tenant = get_tenant(session, tenant_id)
        key_def = get_key(session, key)
        new_value = coerce_value(key_def, value)
        check_entitled(session, tenant, key_def)
        missing = []
        pages = []
        for name in page_names_:
            try:
                pages.append(get_page(session, tenant, name))
            except PageNotFound:
                missing.append(name)
        if missing:
            raise PageNotFound(", ".join(missing), page_names(session, tenant.id))
        return [
            apply_setting(
                session,
                tenant,
                page,
                key_def,
                new_value,
                actor=actor,
                request_id=request_id,
                action="bulk_set_setting",
            )
            for page in pages
        ]


def apply_template(tenant_id: str, template_name: str, *, actor: str = "system", request_id: str | None = None) -> str:
    """Switch the tenant's base template; page and tenant overrides still win over it."""
    with session_scope() as session:
        tenant = get_tenant(session, tenant_id)
        template = get_template(session, template_name)
        before = tenant.template.name if tenant.template else None
        tenant.template_id = template.id
        events.record(
            session,
            tenant_id=tenant.id,
            actor=actor,
            action="apply_template",
            event_type="tenant.template_changed",
            before=before,
            after=template.name,
            request_id=request_id,
        )
        return template.name


# --- Previews: validate and describe a gated action without changing anything ---


def preview_delete_page(tenant_id: str, name: str) -> tuple[str, str]:
    """Returns (canonical page name, summary)."""
    with session_scope() as session:
        tenant = get_tenant(session, tenant_id)
        page = get_page(session, tenant, name)
        overrides = len(list(session.scalars(select(Setting.id).where(Setting.page_id == page.id))))
        return page.name, f"Delete page '{page.name}' and its {overrides} setting override(s)."


def preview_bulk_set_setting(
    tenant_id: str, page_names_: list[str], key: str, value: Any
) -> tuple[list[str], Any, str]:
    """Returns (canonical page names, normalised value, summary)."""
    with session_scope() as session:
        tenant = get_tenant(session, tenant_id)
        key_def = get_key(session, key)
        new_value = coerce_value(key_def, value)
        check_entitled(session, tenant, key_def)
        missing, pages = [], []
        for name in page_names_:
            try:
                pages.append(get_page(session, tenant, name))
            except PageNotFound:
                missing.append(name)
        if missing:
            raise PageNotFound(", ".join(missing), page_names(session, tenant.id))
        changing = [p.name for p in pages if resolve_all(session, tenant, p)[key_def.name].value != new_value]
        summary = (
            f"Set {key_def.name}={new_value!r} on {len(pages)} page(s) ({', '.join(p.name for p in pages)}); "
            f"{len(changing)} will change."
        )
        return [p.name for p in pages], new_value, summary


def preview_apply_template(tenant_id: str, template_name: str) -> tuple[str, str]:
    """Returns (canonical template name, summary)."""
    with session_scope() as session:
        tenant = get_tenant(session, tenant_id)
        template = get_template(session, template_name)
        current = tenant.template.name if tenant.template else "none"
        return template.name, f"Switch the tenant template from '{current}' to '{template.name}'."
