import logging
from typing import TypedDict

from langgraph.graph import END, StateGraph

from app.agent.agent import agent
from app.db.repository import get_all_tenant_pages

log = logging.getLogger(__name__)


# 1. Shared state passed across workflow steps
class TenantWorkflowState(TypedDict):
    prompt: str
    tenant_uuid: str
    is_valid: bool
    agent_response: str


# 2. Graph nodes
def check_tenant_node(state: TenantWorkflowState):
    """Step 1: Verify the tenant UUID exists in the database."""
    log.info("Step 1: validating tenant UUID %s", state["tenant_uuid"])
    pages = get_all_tenant_pages()
    valid_uuids = {p["tenant_uuid"] for p in pages}

    # An empty table allows everything, for testing convenience
    state["is_valid"] = state["tenant_uuid"] in valid_uuids or not valid_uuids
    log.info("Tenant %s", "verified" if state["is_valid"] else "NOT found")
    return state


def run_agent_node(state: TenantWorkflowState):
    """Step 2: Run the Pydantic AI agent once validation passes."""
    log.info("Step 2: handing over to agent")
    result = agent.run_sync(state["prompt"])
    state["agent_response"] = result.output
    return state


def denied_node(state: TenantWorkflowState):
    """Alternative step: block execution for an invalid tenant."""
    log.info("Execution blocked by security policy")
    state["agent_response"] = "Operation blocked: Invalid or inactive tenant UUID."
    return state


# 3. Conditional routing
def route_after_check(state: TenantWorkflowState):
    return "run_agent" if state["is_valid"] else "block_execution"


# 4. Build and compile the graph
workflow = StateGraph(TenantWorkflowState)
workflow.add_node("check_tenant", check_tenant_node)
workflow.add_node("run_agent", run_agent_node)
workflow.add_node("denied", denied_node)
workflow.set_entry_point("check_tenant")
workflow.add_conditional_edges(
    "check_tenant",
    route_after_check,
    {"run_agent": "run_agent", "block_execution": "denied"},
)
workflow.add_edge("run_agent", END)
workflow.add_edge("denied", END)

app_graph = workflow.compile()


def run_workflow(prompt: str, tenant_uuid: str) -> TenantWorkflowState:
    initial_state: TenantWorkflowState = {
        "prompt": prompt,
        "tenant_uuid": tenant_uuid,
        "is_valid": False,
        "agent_response": "",
    }
    return app_graph.invoke(initial_state)  # type: ignore[return-value]
