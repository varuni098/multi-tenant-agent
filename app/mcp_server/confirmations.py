"""Two-step confirmation for destructive or bulk actions, held by tenant-mcp.

A gated tool validates its arguments and stores them under a single-use token bound to the
tenant and user. Only the agent app's confirm route (purpose "confirm") can execute it, so a
model, whether ours or an external client's, can never approve its own action.
"""

import json
import secrets
from collections.abc import Callable
from typing import Any

from app.core.config import get_settings
from app.harness.context import Identity
from app.harness.policy import role_allows
from app.harness.store import get_store
from app.services import admin


class ConfirmationError(Exception):
    pass


def _delete_page(identity: Identity, args: dict[str, Any], request_id: str) -> str:
    admin.delete_page(identity.tenant_id, args["page_name"], actor=identity.user_email, request_id=request_id)
    return f"Deleted page '{args['page_name']}'."


def _bulk_set_setting(identity: Identity, args: dict[str, Any], request_id: str) -> str:
    results = admin.bulk_set_setting(
        identity.tenant_id,
        args["page_names"],
        args["key"],
        args["value"],
        actor=identity.user_email,
        request_id=request_id,
    )
    changed = ", ".join(str(r.page_name) for r in results if r.changed) or "none"
    return f"Set {args['key']}={args['value']!r}; changed pages: {changed}."


def _apply_template(identity: Identity, args: dict[str, Any], request_id: str) -> str:
    name = admin.apply_template(
        identity.tenant_id, args["template_name"], actor=identity.user_email, request_id=request_id
    )
    return f"Applied template '{name}'."


EXECUTORS: dict[str, Callable[[Identity, dict[str, Any], str], str]] = {
    "delete_page": _delete_page,
    "bulk_set_setting": _bulk_set_setting,
    "apply_template": _apply_template,
}


def request_confirmation(identity: Identity, tool: str, args: dict[str, Any], summary: str) -> dict[str, Any]:
    ttl = get_settings().confirmation_ttl_seconds
    token = secrets.token_urlsafe(16)
    record = {
        "tool": tool,
        "args": args,
        "summary": summary,
        "tenant_id": identity.tenant_id,
        "user_email": identity.user_email,
    }
    get_store().set(f"confirm:{token}", json.dumps(record), ttl)
    return {"confirmation_required": True, "token": token, "summary": summary, "expires_in": ttl}


def confirm(identity: Identity, token: str, request_id: str) -> str:
    raw = get_store().pop(f"confirm:{token}")  # single use, even if the checks below fail
    if raw is None:
        raise ConfirmationError("This confirmation is invalid, expired or already used.")
    record = json.loads(raw)
    if (record["tenant_id"], record["user_email"]) != (identity.tenant_id, identity.user_email):
        raise ConfirmationError("This confirmation belongs to a different user.")
    if not role_allows(identity.role, record["tool"]):
        raise ConfirmationError("You are no longer allowed to perform this action.")
    return EXECUTORS[record["tool"]](identity, record["args"], request_id)
