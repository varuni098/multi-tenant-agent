"""Role-based tool access. The model only ever sees the tools this policy allows for the request."""

from dataclasses import dataclass
from typing import Literal

from app.harness.context import ROLE_RANK, Role

Intent = Literal["read", "write", "off_topic"]


@dataclass(frozen=True)
class ToolPolicy:
    min_role: Role
    writes: bool = False
    needs_confirmation: bool = False
    server: Literal["tenant", "graph"] = "tenant"
    agent: bool = True  # offered to our agent (False = external MCP clients only, keeps the model's list short)


TOOL_POLICIES: dict[str, ToolPolicy] = {
    # Read
    "list_pages": ToolPolicy("viewer"),
    "search_pages": ToolPolicy("viewer"),
    "get_theme": ToolPolicy("viewer"),
    "get_settings_for_page": ToolPolicy("viewer"),
    "get_change_history": ToolPolicy("editor"),
    # Write
    "set_theme": ToolPolicy("editor", writes=True),
    "set_setting": ToolPolicy("editor", writes=True),
    "undo_last_change": ToolPolicy("editor", writes=True),
    # Admin
    "create_page": ToolPolicy("admin", writes=True),
    "delete_page": ToolPolicy("admin", writes=True, needs_confirmation=True),
    "bulk_set_setting": ToolPolicy("admin", writes=True, needs_confirmation=True),
    "apply_template": ToolPolicy("admin", writes=True, needs_confirmation=True),
    # Knowledge graph (graph-mcp), all read-only
    "explain_setting": ToolPolicy("viewer", server="graph"),
    "can_apply": ToolPolicy("viewer", server="graph"),
    "impact_of_change": ToolPolicy("admin", server="graph"),
    "search_docs": ToolPolicy("viewer", server="graph"),
    "graph_effective_settings": ToolPolicy("viewer", server="graph", agent=False),
    "resolve_entity": ToolPolicy("viewer", server="graph", agent=False),
    "find_related": ToolPolicy("viewer", server="graph", agent=False),
}


def role_allows(role: Role, tool: str) -> bool:
    policy = TOOL_POLICIES.get(tool)
    return policy is not None and ROLE_RANK[role] >= ROLE_RANK[policy.min_role]


def allowed_tools(role: Role, intent: Intent, graph: bool = False) -> frozenset[str]:
    """Tools our agent may be offered: by role, read-only requests lose every write tool,
    and graph tools only when the knowledge graph is enabled."""
    return frozenset(
        name
        for name, policy in TOOL_POLICIES.items()
        if role_allows(role, name)
        and policy.agent
        and not (intent == "read" and policy.writes)
        and (graph or policy.server != "graph")
    )


def server_tools(server: Literal["tenant", "graph"]) -> list[str]:
    return [name for name, policy in TOOL_POLICIES.items() if policy.server == server]
