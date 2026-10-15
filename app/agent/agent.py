import logging
import os

# Suppress the Pydantic AI startup banner
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

from collections.abc import Callable

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider

from app.core.config import get_settings
from app.services import settings as svc
from app.services.errors import DomainError, InvalidSettingValue, PageNotFound, UnknownSettingKey
from app.services.schemas import AuditEntry, ChangeResult, PageInfo, SettingValue

log = logging.getLogger(__name__)
settings = get_settings()

# Local LLM served by Ollama (OpenAI-compatible API)
local_model = OpenAIChatModel(
    model_name=settings.ollama_model,
    provider=OllamaProvider(base_url=settings.ollama_base_url),
)

agent = Agent(
    model=local_model,
    retries=2,
    system_prompt=(
        "You are a strict multi-tenant configuration assistant. "
        "CRITICAL RULE: You MUST always call your tools to read or change data; never guess or invent values. "
        "Pages have settings (theme, language, layout, brand_color, show_tips). A page inherits a setting from "
        "the tenant default, then the tenant's template, unless it has its own override. "
        "To toggle a theme: read it with get_theme, then call set_theme with the opposite value. "
        "If a page name is unclear, use search_pages or list_pages. "
        "Answer briefly using the tool results."
    ),
)

# Errors the model can fix by retrying with different arguments
_RETRYABLE = (PageNotFound, UnknownSettingKey, InvalidSettingValue)


def _call[T](name: str, fn: Callable[[], T], **args: object) -> T | str:
    """Run a service call: log it, turn fixable errors into a retry hint, other domain errors into a message."""
    log.info("%s(%s)", name, ", ".join(f"{k}={v!r}" for k, v in args.items()))
    try:
        return fn()
    except _RETRYABLE as e:
        raise ModelRetry(str(e)) from e
    except DomainError as e:
        return str(e)


@agent.tool
def list_pages(ctx: RunContext, tenant_uuid: str) -> list[PageInfo] | str:
    """List every page of a tenant with its current theme.

    Args:
        tenant_uuid: The tenant's UUID, copied exactly from the request.
    """
    return _call("list_pages", lambda: svc.list_pages(tenant_uuid), tenant_uuid=tenant_uuid)


@agent.tool
def search_pages(ctx: RunContext, tenant_uuid: str, query: str) -> list[PageInfo] | str:
    """Find pages whose name matches loosely, e.g. 'login screen' finds 'login'.

    Args:
        tenant_uuid: The tenant's UUID, copied exactly from the request.
        query: Words describing the page.
    """
    return _call("search_pages", lambda: svc.search_pages(tenant_uuid, query), tenant_uuid=tenant_uuid, query=query)


@agent.tool
def get_theme(ctx: RunContext, tenant_uuid: str, page_name: str) -> SettingValue | str:
    """Get a page's current theme (light/dark) and where it comes from.

    Args:
        tenant_uuid: The tenant's UUID, copied exactly from the request.
        page_name: The page's short identifier, e.g. 'login' or 'dashboard' (never include the word 'page').
    """
    return _call(
        "get_theme",
        lambda: svc.get_setting(tenant_uuid, "theme", page_name),
        tenant_uuid=tenant_uuid,
        page_name=page_name,
    )


@agent.tool
def set_theme(ctx: RunContext, tenant_uuid: str, page_name: str, theme: str) -> ChangeResult | str:
    """Set a page's theme to an exact value. Safe to repeat: setting the current value changes nothing.

    Args:
        tenant_uuid: The tenant's UUID, copied exactly from the request.
        page_name: The page's short identifier, e.g. 'login' or 'dashboard' (never include the word 'page').
        theme: 'light' or 'dark'.
    """
    return _call(
        "set_theme",
        lambda: svc.set_theme(tenant_uuid, page_name, theme, actor="agent"),
        tenant_uuid=tenant_uuid,
        page_name=page_name,
        theme=theme,
    )


@agent.tool
def get_settings_for_page(ctx: RunContext, tenant_uuid: str, page_name: str | None = None) -> list[SettingValue] | str:
    """Get every effective setting of a page (or tenant-wide defaults when page_name is omitted).

    Args:
        tenant_uuid: The tenant's UUID, copied exactly from the request.
        page_name: The page's short identifier, or omit for tenant-wide defaults.
    """
    return _call(
        "get_settings_for_page",
        lambda: svc.get_effective_settings(tenant_uuid, page_name),
        tenant_uuid=tenant_uuid,
        page_name=page_name,
    )


@agent.tool
def set_setting(
    ctx: RunContext, tenant_uuid: str, key: str, value: str, page_name: str | None = None
) -> ChangeResult | str:
    """Set any setting (language, layout, brand_color, show_tips, theme) on a page or tenant-wide.

    Args:
        tenant_uuid: The tenant's UUID, copied exactly from the request.
        key: Setting name, e.g. 'language' or 'layout'.
        value: New value, e.g. 'fr', 'compact', '#ff0000', 'true'.
        page_name: The page's short identifier, or omit to set the tenant-wide default.
    """
    return _call(
        "set_setting",
        lambda: svc.set_setting(tenant_uuid, key, value, page_name, actor="agent"),
        tenant_uuid=tenant_uuid,
        key=key,
        value=value,
        page_name=page_name,
    )


@agent.tool
def get_change_history(ctx: RunContext, tenant_uuid: str, page_name: str | None = None) -> list[AuditEntry] | str:
    """Recent changes (who changed what, when), newest first. A null before/after means "inherited".

    Args:
        tenant_uuid: The tenant's UUID, copied exactly from the request.
        page_name: Limit to one page, or omit for all pages.
    """
    return _call(
        "get_change_history",
        lambda: svc.get_change_history(tenant_uuid, page_name),
        tenant_uuid=tenant_uuid,
        page_name=page_name,
    )


@agent.tool
def undo_last_change(ctx: RunContext, tenant_uuid: str, page_name: str | None = None) -> ChangeResult | str:
    """Revert the most recent setting change (optionally only for one page).

    Args:
        tenant_uuid: The tenant's UUID, copied exactly from the request.
        page_name: Limit to one page, or omit to undo the latest change anywhere.
    """
    return _call(
        "undo_last_change",
        lambda: svc.undo_last_change(tenant_uuid, page_name, actor="agent"),
        tenant_uuid=tenant_uuid,
        page_name=page_name,
    )
