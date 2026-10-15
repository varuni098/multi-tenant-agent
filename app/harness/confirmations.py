"""Two-step confirmation for destructive or bulk actions.

The agent never executes these. It stores the validated arguments under a single-use
token bound to the tenant and user; confirming runs the stored call directly, with no model.
"""

import json
import secrets
from collections.abc import Callable
from typing import Any

from app.core.config import get_settings
from app.harness.context import Action, Identity, PendingConfirmation, RunDeps
from app.harness.policy import role_allows
from app.harness.store import get_store
from app.services import admin


class ConfirmationError(Exception):
    pass


def _delete_page(identity: Identity, args: dict[str, Any], request_id: str) -> Action:
    admin.delete_page(identity.tenant_id, args["page_name"], actor=identity.user_email, request_id=request_id)
    return Action(tool="delete_page", message=f"Deleted page '{args['page_name']}'.")


def _bulk_set_setting(identity: Identity, args: dict[str, Any], request_id: str) -> Action:
    results = admin.bulk_set_setting(
        identity.tenant_id,
        args["page_names"],
        args["key"],
        args["value"],
        actor=identity.user_email,
        request_id=request_id,
    )
    changed = ", ".join(str(r.page_name) for r in results if r.changed) or "none"
    return Action(
        tool="bulk_set_setting",
        message=f"Set {args['key']}={args['value']!r}; changed pages: {changed}.",
        result=results,
    )


def _apply_template(identity: Identity, args: dict[str, Any], request_id: str) -> Action:
    name = admin.apply_template(
        identity.tenant_id, args["template_name"], actor=identity.user_email, request_id=request_id
    )
    return Action(tool="apply_template", message=f"Applied template '{name}'.")


EXECUTORS: dict[str, Callable[[Identity, dict[str, Any], str], Action]] = {
    "delete_page": _delete_page,
    "bulk_set_setting": _bulk_set_setting,
    "apply_template": _apply_template,
}


def request_confirmation(deps: RunDeps, tool: str, args: dict[str, Any], summary: str) -> PendingConfirmation:
    ttl = get_settings().confirmation_ttl_seconds
    token = secrets.token_urlsafe(16)
    record = {
        "tool": tool,
        "args": args,
        "summary": summary,
        "tenant_id": deps.identity.tenant_id,
        "user_email": deps.identity.user_email,
    }
    get_store().set(f"confirm:{token}", json.dumps(record), ttl)
    pending = PendingConfirmation(token=token, tool=tool, summary=summary, expires_in=ttl)
    deps.confirmations.append(pending)
    return pending


def confirm(identity: Identity, token: str, request_id: str) -> Action:
    raw = get_store().pop(f"confirm:{token}")  # single use, even if the checks below fail
    if raw is None:
        raise ConfirmationError("This confirmation is invalid, expired or already used.")
    record = json.loads(raw)
    if (record["tenant_id"], record["user_email"]) != (identity.tenant_id, identity.user_email):
        raise ConfirmationError("This confirmation belongs to a different user.")
    if not role_allows(identity.role, record["tool"]):
        raise ConfirmationError("You are no longer allowed to perform this action.")
    return EXECUTORS[record["tool"]](identity, record["args"], request_id)
