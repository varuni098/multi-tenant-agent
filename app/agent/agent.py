import logging
import os

# Suppress the Pydantic AI startup banner
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider

from app.core.config import get_settings
from app.db.repository import get_tenant_page_theme, toggle_tenant_page_theme

log = logging.getLogger(__name__)
settings = get_settings()

# Local LLM served by Ollama (OpenAI-compatible API)
local_model = OpenAIChatModel(
    model_name=settings.ollama_model,
    provider=OllamaProvider(base_url=settings.ollama_base_url),
)

# Initialize the Pydantic AI agent with an explicit directive to always use tools
agent = Agent(
    model=local_model,
    system_prompt=(
        "You are a strict multi-tenant database assistant. "
        "CRITICAL RULE: You MUST always call your available tools to query or update the database. "
        "Never guess, assume, or hardcode theme statuses. "
        "Always invoke the tools for any theme check or toggle request."
    ),
)


@agent.tool
def tool_get_theme(ctx: RunContext, tenant_uuid: str, page_name: str) -> str:
    """Get the current theme status (light/dark) for a specific tenant UUID and page name.

    Args:
        tenant_uuid: The tenant's UUID, copied exactly from the request.
        page_name: The page's short identifier, e.g. 'login' or 'dashboard' (never include the word 'page').
    """
    log.info("tool_get_theme(tenant_uuid=%r, page_name=%r)", tenant_uuid, page_name)
    return get_tenant_page_theme(tenant_uuid, page_name)


@agent.tool
def tool_toggle_theme(ctx: RunContext, tenant_uuid: str, page_name: str) -> str:
    """Toggle the theme between light and dark mode for a specific tenant page event.

    Args:
        tenant_uuid: The tenant's UUID, copied exactly from the request.
        page_name: The page's short identifier, e.g. 'login' or 'dashboard' (never include the word 'page').
    """
    log.info("tool_toggle_theme(tenant_uuid=%r, page_name=%r)", tenant_uuid, page_name)
    return toggle_tenant_page_theme(tenant_uuid, page_name)
