from collections.abc import Callable
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


@dataclass(frozen=True)
class Caller:
    """An authenticated identity plus what tenant-mcp reported about it at login."""

    identity: Identity
    tenant_active: bool
    page_names: tuple[str, ...] = ()


@dataclass
class Action:
    """A change that actually happened during a run (ground truth for the response)."""

    tool: str
    message: str
    result: Any = None


@dataclass
class Denial:
    """A write the policy gate refused, with the ontology rule that decided it."""

    tool: str
    rule: str
    reason: str


@dataclass
class Citation:
    """A documentation source returned by search_docs during the run."""

    rank: int
    title: str
    heading: str
    path: str


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
    denials: list[Denial] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    verify_retries: int = 0
    verification_failed: bool = False
    # Progress events for streaming clients: emit(event_type, data). Never carries tokens or secrets.
    emit: Callable[[str, dict[str, Any]], None] | None = None

    def event(self, event_type: str, **data: Any) -> None:
        if self.emit is not None:
            self.emit(event_type, data)
