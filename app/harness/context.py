from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["viewer", "editor", "admin"]
ROLE_RANK: dict[str, int] = {"viewer": 0, "editor": 1, "admin": 2}


@dataclass(frozen=True)
class Identity:
    """Who is asking. Comes from the verified token, never from the prompt or the model."""

    tenant_id: str
    user_email: str
    role: Role


@dataclass
class Action:
    """A change that actually happened during a run (ground truth for the response)."""

    tool: str
    message: str
    result: Any = None


@dataclass
class PendingConfirmation:
    token: str
    tool: str
    summary: str
    expires_in: int


@dataclass
class RunDeps:
    """Per-run dependencies handed to the agent's tools via RunContext.deps."""

    identity: Identity
    request_id: str
    allowed_tools: frozenset[str]
    actions: list[Action] = field(default_factory=list)
    confirmations: list[PendingConfirmation] = field(default_factory=list)
    verify_retries: int = 0
    verification_failed: bool = False
