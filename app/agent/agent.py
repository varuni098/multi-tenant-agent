"""The configuration agent.

Tools never take a tenant argument: the tenant and user come from the verified
identity in ctx.deps, so the model cannot address another tenant's data.
"""

import logging
import os

# Suppress the Pydantic AI startup banner
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

from collections.abc import Callable

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.tools import ToolDefinition

from app.harness.confirmations import request_confirmation
from app.harness.context import Action, RunDeps
from app.harness.guardrails import claims_change, redact
from app.services import admin
from app.services import settings as svc
from app.services.errors import DomainError, InvalidSettingValue, PageNotFound, TemplateNotFound, UnknownSettingKey
from app.services.schemas import AuditEntry, ChangeResult, PageInfo, SettingValue

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a strict configuration assistant for one tenant's pages. "
    "CRITICAL RULE: always call your tools to read or change data; never guess or invent values, plans or pages. "
    "Pages have settings (theme, language, layout, brand_color, show_tips). A page inherits a setting from "
    "the tenant default, then the tenant's template, unless it has its own override. "
    "To toggle a theme: read it with get_theme, then call set_theme with the opposite value. "
    "If a page name is unclear, use search_pages or list_pages. "
    "If a tool you need is not available, say you are not permitted to do that. "
    "If a tool says confirmation is required, tell the user what will happen and that they must confirm. "
    "Answer briefly using the tool results."
)

# The model is chosen per run by the harness (see app/harness/reliability.py)
agent = Agent(deps_type=RunDeps, retries=2, system_prompt=SYSTEM_PROMPT)

# Errors the model can fix by retrying with different arguments
_RETRYABLE = (PageNotFound, UnknownSettingKey, InvalidSettingValue, TemplateNotFound)


async def _permitted(ctx: RunContext[RunDeps], tool_def: ToolDefinition) -> ToolDefinition | None:
    """Hide tools the policy doesn't allow for this request."""
    return tool_def if tool_def.name in ctx.deps.allowed_tools else None


def _call[T](ctx: RunContext[RunDeps], name: str, fn: Callable[[], T], **args: object) -> T | str:
    """Run a service call: enforce policy, log, record changes, map domain errors for the model."""
    if name not in ctx.deps.allowed_tools:  # defence in depth; _permitted already hides it
        return "You are not permitted to do that."
    log.info("[%s] %s(%s)", ctx.deps.request_id, name, redact(", ".join(f"{k}={v!r}" for k, v in args.items())))
    try:
        result = fn()
    except _RETRYABLE as e:
        raise ModelRetry(str(e)) from e
    except DomainError as e:
        return str(e)
    if isinstance(result, ChangeResult) and result.changed:
        ctx.deps.actions.append(Action(tool=name, message=result.message, result=result))
    return result


def _tenant(ctx: RunContext[RunDeps]) -> str:
    return ctx.deps.identity.tenant_id


def _actor(ctx: RunContext[RunDeps]) -> dict:
    return {"actor": ctx.deps.identity.user_email, "request_id": ctx.deps.request_id}


# --- Read tools ---


@agent.tool(prepare=_permitted)
def list_pages(ctx: RunContext[RunDeps]) -> list[PageInfo] | str:
    """List every page with its current theme."""
    return _call(ctx, "list_pages", lambda: svc.list_pages(_tenant(ctx)))


@agent.tool(prepare=_permitted)
def search_pages(ctx: RunContext[RunDeps], query: str) -> list[PageInfo] | str:
    """Find pages whose name matches loosely, e.g. 'login screen' finds 'login'.

    Args:
        query: Words describing the page.
    """
    return _call(ctx, "search_pages", lambda: svc.search_pages(_tenant(ctx), query), query=query)


@agent.tool(prepare=_permitted)
def get_theme(ctx: RunContext[RunDeps], page_name: str) -> SettingValue | str:
    """Get a page's current theme (light/dark) and where it comes from.

    Args:
        page_name: The page's short identifier, e.g. 'login' or 'dashboard' (never include the word 'page').
    """
    return _call(ctx, "get_theme", lambda: svc.get_setting(_tenant(ctx), "theme", page_name), page_name=page_name)


@agent.tool(prepare=_permitted)
def get_settings_for_page(ctx: RunContext[RunDeps], page_name: str | None = None) -> list[SettingValue] | str:
    """Get every effective setting of a page (or tenant-wide defaults when page_name is omitted).

    Args:
        page_name: The page's short identifier, or omit for tenant-wide defaults.
    """
    return _call(
        ctx,
        "get_settings_for_page",
        lambda: svc.get_effective_settings(_tenant(ctx), page_name),
        page_name=page_name,
    )


@agent.tool(prepare=_permitted)
def get_change_history(ctx: RunContext[RunDeps], page_name: str | None = None) -> list[AuditEntry] | str:
    """Recent changes (who changed what, when), newest first. A null before/after means "inherited".

    Args:
        page_name: Limit to one page, or omit for all pages.
    """
    return _call(
        ctx, "get_change_history", lambda: svc.get_change_history(_tenant(ctx), page_name), page_name=page_name
    )


# --- Write tools ---


@agent.tool(prepare=_permitted)
def set_theme(ctx: RunContext[RunDeps], page_name: str, theme: str) -> ChangeResult | str:
    """Set a page's theme to an exact value. Safe to repeat: setting the current value changes nothing.

    Args:
        page_name: The page's short identifier, e.g. 'login' or 'dashboard' (never include the word 'page').
        theme: 'light' or 'dark'.
    """
    return _call(
        ctx,
        "set_theme",
        lambda: svc.set_theme(_tenant(ctx), page_name, theme, **_actor(ctx)),
        page_name=page_name,
        theme=theme,
    )


@agent.tool(prepare=_permitted)
def set_setting(ctx: RunContext[RunDeps], key: str, value: str, page_name: str | None = None) -> ChangeResult | str:
    """Set any setting (language, layout, brand_color, show_tips, theme) on a page or tenant-wide.

    Args:
        key: Setting name, e.g. 'language' or 'layout'.
        value: New value, e.g. 'fr', 'compact', '#ff0000', 'true'.
        page_name: The page's short identifier, or omit to set the tenant-wide default.
    """
    return _call(
        ctx,
        "set_setting",
        lambda: svc.set_setting(_tenant(ctx), key, value, page_name, **_actor(ctx)),
        key=key,
        value=value,
        page_name=page_name,
    )


@agent.tool(prepare=_permitted)
def undo_last_change(ctx: RunContext[RunDeps], page_name: str | None = None) -> ChangeResult | str:
    """Revert the most recent setting change (optionally only for one page).

    Args:
        page_name: Limit to one page, or omit to undo the latest change anywhere.
    """
    return _call(
        ctx,
        "undo_last_change",
        lambda: svc.undo_last_change(_tenant(ctx), page_name, **_actor(ctx)),
        page_name=page_name,
    )


# --- Admin tools ---


@agent.tool(prepare=_permitted)
def create_page(ctx: RunContext[RunDeps], page_name: str, theme: str | None = None) -> PageInfo | str:
    """Create a new page.

    Args:
        page_name: Short identifier for the new page, e.g. 'billing'.
        theme: Optional starting theme, 'light' or 'dark'.
    """

    def run() -> PageInfo:
        page = admin.create_page(_tenant(ctx), page_name, theme, **_actor(ctx))
        ctx.deps.actions.append(Action(tool="create_page", message=f"Created page '{page.name}'.", result=page))
        return page

    return _call(ctx, "create_page", run, page_name=page_name, theme=theme)


def _gate(ctx: RunContext[RunDeps], tool: str, args: dict, summary: str) -> str:
    pending = request_confirmation(ctx.deps, tool, args, summary)
    return f"Confirmation required (not done yet): {pending.summary} The user must confirm before it happens."


@agent.tool(prepare=_permitted)
def delete_page(ctx: RunContext[RunDeps], page_name: str) -> str:
    """Delete a page and its overrides. Requires the user's confirmation.

    Args:
        page_name: The page's short identifier.
    """

    def run() -> str:
        name, summary = admin.preview_delete_page(_tenant(ctx), page_name)
        return _gate(ctx, "delete_page", {"page_name": name}, summary)

    return _call(ctx, "delete_page", run, page_name=page_name)


@agent.tool(prepare=_permitted)
def bulk_set_setting(ctx: RunContext[RunDeps], page_names: list[str], key: str, value: str) -> str:
    """Set one setting on several pages at once. Requires the user's confirmation.

    Args:
        page_names: Short identifiers of the pages.
        key: Setting name, e.g. 'theme'.
        value: New value.
    """

    def run() -> str:
        names, normalised, summary = admin.preview_bulk_set_setting(_tenant(ctx), page_names, key, value)
        return _gate(ctx, "bulk_set_setting", {"page_names": names, "key": key, "value": normalised}, summary)

    return _call(ctx, "bulk_set_setting", run, page_names=page_names, key=key, value=value)


@agent.tool(prepare=_permitted)
def apply_template(ctx: RunContext[RunDeps], template_name: str) -> str:
    """Switch the tenant's base template ('standard' or 'dark_first'). Requires the user's confirmation.

    Args:
        template_name: Template name.
    """

    def run() -> str:
        name, summary = admin.preview_apply_template(_tenant(ctx), template_name)
        return _gate(ctx, "apply_template", {"template_name": name}, summary)

    return _call(ctx, "apply_template", run, template_name=template_name)


# --- Output verification ---


@agent.output_validator
def verify_claims(ctx: RunContext[RunDeps], output: str) -> str:
    """Reject answers that claim a change when no change happened; one retry, then flag for a safe reply."""
    if claims_change(output) and not ctx.deps.actions and not ctx.deps.confirmations:
        if ctx.deps.verify_retries < 1:
            ctx.deps.verify_retries += 1
            raise ModelRetry(
                "Your answer says something was changed, but no change tool succeeded. "
                "Call the right tool, or answer truthfully that nothing was changed."
            )
        ctx.deps.verification_failed = True
    return output
