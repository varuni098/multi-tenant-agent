"""Read and change tenant pages and settings.

Effective value of a setting = page override > tenant default > tenant's template > key default.
"""

import re
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Page, PlanFeature, Setting, SettingKey, Template, TemplateSetting, Tenant
from app.db.session import session_scope
from app.services import events
from app.services.errors import (
    InvalidSettingValue,
    NotEntitled,
    NothingToUndo,
    PageNotFound,
    TemplateNotFound,
    TenantNotFound,
    UnknownSettingKey,
)
from app.services.schemas import AuditEntry, ChangeResult, PageInfo, SettingValue

UNDOABLE_ACTIONS = ("set_setting", "bulk_set_setting")
_TRUE = {"true", "yes", "on", "1", "enabled"}
_FALSE = {"false", "no", "off", "0", "disabled"}


# --- Lookups ---


def get_tenant(session: Session, tenant_id: str) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise TenantNotFound(tenant_id)
    return tenant


def page_names(session: Session, tenant_id: str) -> list[str]:
    return list(session.scalars(select(Page.name).where(Page.tenant_id == tenant_id).order_by(Page.name)))


def get_page(session: Session, tenant: Tenant, page_name: str) -> Page:
    page = session.scalars(
        select(Page).where(Page.tenant_id == tenant.id, func.lower(Page.name) == page_name.strip().lower())
    ).first()
    if page is None:
        raise PageNotFound(page_name, page_names(session, tenant.id))
    return page


def get_key(session: Session, key: str) -> SettingKey:
    key_def = session.get(SettingKey, key.strip().lower())
    if key_def is None:
        raise UnknownSettingKey(key, list(session.scalars(select(SettingKey.name).order_by(SettingKey.name))))
    return key_def


def override_row(session: Session, tenant_id: str, page: Page | None, key: str, lock: bool = False) -> Setting | None:
    page_filter = Setting.page_id.is_(None) if page is None else Setting.page_id == page.id
    stmt = select(Setting).where(Setting.tenant_id == tenant_id, page_filter, Setting.setting_key == key)
    if lock:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


# --- Resolution ---


def resolve_all(session: Session, tenant: Tenant, page: Page | None) -> dict[str, SettingValue]:
    """Effective value of every setting for a page (or tenant-wide when page is None)."""
    resolved = {
        k.name: SettingValue(key=k.name, value=k.default_value, source="default")
        for k in session.scalars(select(SettingKey))
    }
    if tenant.template_id is not None:
        for ts in session.scalars(select(TemplateSetting).where(TemplateSetting.template_id == tenant.template_id)):
            resolved[ts.setting_key] = SettingValue(key=ts.setting_key, value=ts.value, source="template")

    page_ids: list[int | None] = [None] if page is None else [None, page.id]
    rows = session.scalars(
        select(Setting).where(
            Setting.tenant_id == tenant.id,
            Setting.page_id.is_(None) | Setting.page_id.in_([p for p in page_ids if p is not None]),
        )
    )
    # Tenant defaults first, then page overrides win
    for row in sorted(rows, key=lambda r: r.page_id is not None):
        source = "tenant" if row.page_id is None else "page"
        resolved[row.setting_key] = SettingValue(key=row.setting_key, value=row.value, source=source)
    return resolved


# --- Validation ---


def coerce_value(key_def: SettingKey, value: Any) -> Any:
    """Validate and normalise a value for a setting (e.g. 'Dark' -> 'dark', 'yes' -> True)."""
    if key_def.value_type == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise InvalidSettingValue(key_def.name, value, "true or false")

    text = str(value).strip()
    if key_def.value_type == "enum":
        allowed = key_def.allowed_values or []
        for option in allowed:
            if text.lower() == str(option).lower():
                return option
        raise InvalidSettingValue(key_def.name, value, f"one of {', '.join(map(str, allowed))}")

    if key_def.pattern and not re.fullmatch(key_def.pattern, text):
        raise InvalidSettingValue(key_def.name, value, f"a value matching {key_def.pattern}")
    return text


def check_entitled(session: Session, tenant: Tenant, key_def: SettingKey) -> None:
    feature = key_def.required_feature
    if feature is None:
        return
    included = session.get(PlanFeature, (tenant.plan_id, feature.id))
    if included is None:
        raise NotEntitled(key_def.name, feature.key, tenant.plan.name)


# --- Writes (shared with admin services) ---


def apply_setting(
    session: Session,
    tenant: Tenant,
    page: Page | None,
    key_def: SettingKey,
    new_value: Any,
    *,
    actor: str,
    request_id: str | None,
    action: str = "set_setting",
) -> ChangeResult:
    """Set an override in the caller's transaction. No-op if the effective value already matches."""
    page_name = page.name if page else None
    before = resolve_all(session, tenant, page)[key_def.name].value
    if before == new_value:
        return ChangeResult(
            page_name=page_name,
            key=key_def.name,
            before=before,
            after=before,
            changed=False,
            message=f"'{key_def.name}' is already {new_value!r}.",
        )

    row = override_row(session, tenant.id, page, key_def.name, lock=True)
    override_before = row.value if row else None
    if row:
        row.value = new_value
    else:
        session.add(
            Setting(tenant_id=tenant.id, page_id=page.id if page else None, setting_key=key_def.name, value=new_value)
        )
    events.record(
        session,
        tenant_id=tenant.id,
        actor=actor,
        action=action,
        event_type="setting.changed",
        page_name=page_name,
        setting_key=key_def.name,
        before=override_before,
        after=new_value,
        request_id=request_id,
    )
    return ChangeResult(
        page_name=page_name,
        key=key_def.name,
        before=before,
        after=new_value,
        changed=True,
        message=f"Changed '{key_def.name}' from {before!r} to {new_value!r}.",
    )


# --- Public API (one transaction each) ---


def list_pages(tenant_id: str) -> list[PageInfo]:
    with session_scope() as session:
        tenant = get_tenant(session, tenant_id)
        pages = session.scalars(select(Page).where(Page.tenant_id == tenant.id).order_by(Page.name))
        return [PageInfo(name=p.name, theme=resolve_all(session, tenant, p)["theme"].value) for p in pages]


def search_pages(tenant_id: str, query: str, limit: int = 5) -> list[PageInfo]:
    """Fuzzy page search, e.g. 'login screen' -> login, 'dash' -> dashboard."""
    query = query.strip().lower()
    q_tokens = set(re.findall(r"\w+", query))

    def score(name: str) -> float:
        n = name.lower()
        if n in query or query in n:
            return 1.0
        token_hit = 1.0 if q_tokens & set(re.findall(r"\w+", n)) else 0.0
        return max(SequenceMatcher(None, query, n).ratio(), token_hit)

    ranked = sorted(((score(p.name), p) for p in list_pages(tenant_id)), key=lambda t: t[0], reverse=True)
    return [p for s, p in ranked if s >= 0.5][:limit]


def get_setting(tenant_id: str, key: str, page_name: str | None = None) -> SettingValue:
    with session_scope() as session:
        tenant = get_tenant(session, tenant_id)
        key_def = get_key(session, key)
        page = get_page(session, tenant, page_name) if page_name else None
        return resolve_all(session, tenant, page)[key_def.name]


def get_effective_settings(tenant_id: str, page_name: str | None = None) -> list[SettingValue]:
    with session_scope() as session:
        tenant = get_tenant(session, tenant_id)
        page = get_page(session, tenant, page_name) if page_name else None
        return sorted(resolve_all(session, tenant, page).values(), key=lambda s: s.key)


def set_setting(
    tenant_id: str,
    key: str,
    value: Any,
    page_name: str | None = None,
    *,
    actor: str = "system",
    request_id: str | None = None,
) -> ChangeResult:
    with session_scope() as session:
        tenant = get_tenant(session, tenant_id)
        key_def = get_key(session, key)
        new_value = coerce_value(key_def, value)
        check_entitled(session, tenant, key_def)
        page = get_page(session, tenant, page_name) if page_name else None
        return apply_setting(session, tenant, page, key_def, new_value, actor=actor, request_id=request_id)


def set_theme(
    tenant_id: str, page_name: str, theme: str, *, actor: str = "system", request_id: str | None = None
) -> ChangeResult:
    return set_setting(tenant_id, "theme", theme, page_name, actor=actor, request_id=request_id)


def get_change_history(
    tenant_id: str,
    page_name: str | None = None,
    limit: int = 10,
    *,
    before_id: int | None = None,
    actor: str | None = None,
) -> list[AuditEntry]:
    with session_scope() as session:
        get_tenant(session, tenant_id)
        stmt = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
        if page_name:
            stmt = stmt.where(func.lower(AuditLog.page_name) == page_name.strip().lower())
        if before_id:
            stmt = stmt.where(AuditLog.id < before_id)
        if actor:
            stmt = stmt.where(AuditLog.actor == actor)
        rows = session.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit))
        return [
            AuditEntry(
                id=r.id,
                action=r.action,
                page_name=r.page_name,
                key=r.setting_key,
                before=r.before,
                after=r.after,
                actor=r.actor,
                at=r.created_at,
            )
            for r in rows
        ]


def undo_last_change(
    tenant_id: str, page_name: str | None = None, *, actor: str = "system", request_id: str | None = None
) -> ChangeResult:
    """Revert the most recent setting change not already undone (optionally for one page)."""
    with session_scope() as session:
        tenant = get_tenant(session, tenant_id)
        reverted = select(AuditLog.reverts_id).where(AuditLog.tenant_id == tenant.id, AuditLog.reverts_id.is_not(None))
        stmt = select(AuditLog).where(
            AuditLog.tenant_id == tenant.id,
            AuditLog.action.in_(UNDOABLE_ACTIONS),
            AuditLog.id.not_in(reverted),
        )
        if page_name:
            stmt = stmt.where(func.lower(AuditLog.page_name) == page_name.strip().lower())
        target = session.scalars(stmt.order_by(AuditLog.id.desc()).limit(1)).first()
        if target is None or target.setting_key is None:
            raise NothingToUndo()

        page = get_page(session, tenant, target.page_name) if target.page_name else None
        key = target.setting_key
        before = resolve_all(session, tenant, page)[key].value
        row = override_row(session, tenant.id, page, key, lock=True)
        current_override = row.value if row else None

        if target.before is None:
            if row:
                session.delete(row)  # back to inherited
        elif row:
            row.value = target.before
        else:
            session.add(
                Setting(tenant_id=tenant.id, page_id=page.id if page else None, setting_key=key, value=target.before)
            )
        session.flush()

        after = resolve_all(session, tenant, page)[key].value
        events.record(
            session,
            tenant_id=tenant.id,
            actor=actor,
            action="undo",
            event_type="setting.changed",
            page_name=target.page_name,
            setting_key=key,
            before=current_override,
            after=target.before,
            reverts_id=target.id,
            request_id=request_id,
        )
        return ChangeResult(
            page_name=target.page_name,
            key=key,
            before=before,
            after=after,
            changed=before != after,
            message=f"Reverted '{key}' from {before!r} to {after!r}.",
        )


def get_template(session: Session, name: str) -> Template:
    template = session.scalars(select(Template).where(Template.name == name.strip().lower())).first()
    if template is None:
        raise TemplateNotFound(name, list(session.scalars(select(Template.name).order_by(Template.name))))
    return template
