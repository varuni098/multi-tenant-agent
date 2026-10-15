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
}


def role_allows(role: Role, tool: str) -> bool:
    policy = TOOL_POLICIES.get(tool)
    return policy is not None and ROLE_RANK[role] >= ROLE_RANK[policy.min_role]


def allowed_tools(role: Role, intent: Intent) -> frozenset[str]:
    """Tools for this role; read-only requests also lose every write tool."""
    return frozenset(
        name
        for name, policy in TOOL_POLICIES.items()
        if role_allows(role, name) and not (intent == "read" and policy.writes)
    )
