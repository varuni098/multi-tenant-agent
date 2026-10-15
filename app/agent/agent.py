"""The configuration agent.

Its tools come from tenant-mcp, one MCP connection per run (see app/harness/tenant_client.py).
Tools never take a tenant argument: the tenant and user travel in the signed request context,
so the model cannot address another tenant's data.
"""

import os

# Suppress the Pydantic AI startup banner
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

from pydantic_ai import Agent, ModelRetry, RunContext

from app.harness.context import RunDeps
from app.harness.guardrails import claims_change

SYSTEM_PROMPT = (
    "You are a strict configuration assistant for one tenant's pages. "
    "CRITICAL RULE: always call your tools to read or change data; never guess or invent values, plans or pages. "
    "Pages have settings (theme, language, layout, brand_color, show_tips). A page inherits a setting from "
    "the tenant default, then the tenant's template, unless it has its own override. "
    "To toggle a theme: read it with get_theme, then call set_theme with the opposite value. "
    "If a page name is unclear, use search_pages or list_pages. "
    "When asked why a setting has its value, use explain_setting if available. "
    "When asked whether a change is allowed, use can_apply if available and quote its reason. "
    "For how-to, product or policy questions, use search_docs if available, answer only from its excerpts, "
    "and cite the sources you used by their [number]. "
    "If a tool you need is not available, say you are not permitted to do that. "
    "If a tool says confirmation is required, tell the user what will happen and that they must confirm. "
    "Answer briefly using the tool results."
)

# The model and toolset are chosen per run by the harness (see app/harness/reliability.py)
agent = Agent(deps_type=RunDeps, retries=2, system_prompt=SYSTEM_PROMPT)


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
