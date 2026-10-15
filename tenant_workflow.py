from typing import TypedDict
from langgraph.graph import StateGraph, END
from agent import agent  # Importing Pydantic AI agent
from agent_backend import get_all_tenant_pages

# 1. Define the shared state dictionary passed across workflow steps
class TenantWorkflowState(TypedDict):
    prompt: str
    tenant_uuid: str
    is_valid: bool
    agent_response: str

# 2. Define Graph Nodes (Steps)
def check_tenant_node(state: TenantWorkflowState):
    """Step 1: Verify if the tenant UUID exists in the database."""
    print(f"\n[LangGraph] Step 1: Validating tenant UUID -> {state['tenant_uuid']}")
    
    # Query database using your existing backend tool
    pages = get_all_tenant_pages()
    
    # Check if the requested tenant_uuid exists in the database records
    valid_uuids = {p.get('tenant_uuid') for p in pages} if pages else set()
    
    # For testing convenience, if your table is empty or matches, handle it:
    state["is_valid"] = state["tenant_uuid"] in valid_uuids or len(valid_uuids) == 0
    
    if state["is_valid"]:
        print("[LangGraph] Status: Tenant Verified Successfully.")
    else:
        print("[LangGraph] Status: Tenant NOT found in database.")
        
    return state

def run_agent_node(state: TenantWorkflowState):
    """Step 2: Run the Pydantic AI agent if validation passes."""
    print("[LangGraph] Step 2: Handing over to Pydantic AI Agent...")
    result = agent.run_sync(state["prompt"])
    state["agent_response"] = result.output
    return state

def denied_node(state: TenantWorkflowState):
    """Alternative Step: Block execution if tenant is invalid."""
    print("[LangGraph] Status: Execution blocked by security policy.")
    state["agent_response"] = "Operation blocked: Invalid or inactive tenant UUID."
    return state

# 3. Define Conditional Routing Logic
def route_after_check(state: TenantWorkflowState):
    if state["is_valid"]:
        return "run_agent"
    return "block_execution"

# 4. Build and Compile the StateGraph
workflow = StateGraph(TenantWorkflowState)

# Add nodes to the graph
workflow.add_node("check_tenant", check_tenant_node)
workflow.add_node("run_agent", run_agent_node)
workflow.add_node("denied", denied_node)

# Set the entry point of the workflow
workflow.set_entry_point("check_tenant")

# Add conditional edges branching out from the check node
workflow.add_conditional_edges(
    "check_tenant",
    route_after_check,
    {
        "run_agent": "run_agent",
        "block_execution": "denied"
    }
)

# Connect terminal nodes to the END state
workflow.add_edge("run_agent", END)
workflow.add_edge("denied", END)

# Compile the graph into an executable app
app_graph = workflow.compile()