from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AuditLog, OutboxEvent


def record(
    session: Session,
    *,
    tenant_id: str,
    actor: str,
    action: str,
    event_type: str,
    page_name: str | None = None,
    setting_key: str | None = None,
    before: Any = None,
    after: Any = None,
    reverts_id: int | None = None,
    request_id: str | None = None,
) -> AuditLog:
    """Write an audit entry and an outbox event in the caller's transaction."""
    entry = AuditLog(
        tenant_id=tenant_id,
        actor=actor,
        action=action,
        page_name=page_name,
        setting_key=setting_key,
        before=before,
        after=after,
        reverts_id=reverts_id,
        request_id=request_id,
    )
    session.add(entry)
    session.add(
        OutboxEvent(
            aggregate_type="tenant",
            aggregate_id=tenant_id,
            event_type=event_type,
            payload={
                "tenant_id": tenant_id,
                "page_name": page_name,
                "key": setting_key,
                "before": before,
                "after": after,
                "actor": actor,
                "request_id": request_id,
            },
        )
    )
    return entry
