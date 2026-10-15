import os
import tempfile

# Must be set before any app module reads settings
_db_file = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file}"
os.environ["WARMUP_ON_STARTUP"] = "false"
os.environ["REDIS_URL"] = ""
os.environ["OLLAMA_FALLBACK_MODEL"] = ""

from collections.abc import Callable  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.seed import TENANT_A, TENANT_B, seed  # noqa: E402
from app.db.session import get_engine  # noqa: E402
from app.harness.context import Identity  # noqa: E402
from app.harness.reliability import _candidates  # noqa: E402
from app.harness.store import get_store  # noqa: E402

__all__ = ["TENANT_A", "TENANT_B"]

ADMIN_A = Identity(TENANT_A, "admin@acme.test", "admin")
EDITOR_A = Identity(TENANT_A, "editor@acme.test", "editor")
VIEWER_A = Identity(TENANT_A, "viewer@acme.test", "viewer")
ADMIN_B = Identity(TENANT_B, "admin@globex.test", "admin")


@pytest.fixture(autouse=True)
def db():
    """Fresh, seeded schema for every test.

    TENANT_A (Acme, free plan): login=light, dashboard=dark, settings=inherited (light from template)
      users: admin@acme.test (admin), editor@acme.test (editor), viewer@acme.test (viewer)
    TENANT_B (Globex, pro plan): login=dark; users: admin@globex.test (admin)
    """
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    seed()
    yield


@pytest.fixture(autouse=True)
def fresh_harness_state():
    """New in-memory store and circuit breakers per test."""
    get_store.cache_clear()
    _candidates.cache_clear()
    yield


@pytest.fixture
def settings():
    """The cached settings object; attribute changes are undone after the test."""
    s = get_settings()
    original = s.model_dump()
    yield s
    for key, value in original.items():
        object.__setattr__(s, key, value)


Step = str | list[tuple[str, dict[str, Any]]]


class Script:
    """A FunctionModel that plays scripted steps: a list of tool calls, or a final text answer.

    Records the tool names offered and the message count on each model request.
    """

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.offered: list[list[str]] = []
        self.message_counts: list[int] = []
        self.history: list[list[ModelMessage]] = []
        self.model = FunctionModel(self._respond)

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.offered.append(sorted(t.name for t in info.function_tools))
        self.message_counts.append(len(messages))
        self.history.append(list(messages))
        step = self.steps.pop(0) if self.steps else "done"
        if isinstance(step, str):
            return ModelResponse(parts=[TextPart(step)])
        return ModelResponse(parts=[ToolCallPart(name, args) for name, args in step])

    @property
    def calls(self) -> int:
        return len(self.offered)


@pytest.fixture
def script() -> Callable[..., Script]:
    from app.agent.agent import agent

    overrides = []

    def make(*steps: Step) -> Script:
        s = Script(*steps)
        ctx = agent.override(model=s.model)
        ctx.__enter__()
        overrides.append(ctx)
        return s

    yield make
    for ctx in reversed(overrides):
        ctx.__exit__(None, None, None)
