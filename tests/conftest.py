import os
import socket
import tempfile
import threading
import time


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Must be set before any app module reads settings
_db_file = os.path.join(tempfile.mkdtemp(), "test.db")
_mcp_port = _free_port()
_graph_port = _free_port()
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file}"
os.environ["WARMUP_ON_STARTUP"] = "false"
os.environ["REDIS_URL"] = ""
os.environ["OLLAMA_FALLBACK_MODEL"] = ""
os.environ["TENANT_MCP_URL"] = f"http://127.0.0.1:{_mcp_port}/mcp"
os.environ["GRAPH_MCP_URL"] = f"http://127.0.0.1:{_graph_port}/mcp"
os.environ["GRAPH_MODE"] = "off"  # unit tests run without Neo4j; graph tests opt in via the settings fixture

from collections.abc import Callable  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
import uvicorn  # noqa: E402
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.seed import TENANT_A, TENANT_B, seed  # noqa: E402
from app.db.session import get_engine  # noqa: E402
from app.harness.auth import authenticate, create_token  # noqa: E402
from app.harness.context import Caller, Identity  # noqa: E402
from app.harness.reliability import _candidates  # noqa: E402
from app.harness.store import get_store  # noqa: E402

__all__ = ["TENANT_A", "TENANT_B"]

MCP_URL = os.environ["TENANT_MCP_URL"]
GRAPH_URL = os.environ["GRAPH_MCP_URL"]
GRAPH_PORT = _graph_port


def start_server(app, port: int):
    """Run an ASGI app in a background thread; returns a stop() function."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"test server on port {port} did not start")
        time.sleep(0.05)

    def stop() -> None:
        server.should_exit = True
        thread.join(timeout=5)

    return stop


ADMIN_A = Identity(TENANT_A, "admin@acme.test", "admin")
EDITOR_A = Identity(TENANT_A, "editor@acme.test", "editor")
VIEWER_A = Identity(TENANT_A, "viewer@acme.test", "viewer")
ADMIN_B = Identity(TENANT_B, "admin@globex.test", "admin")


def caller_for(identity: Identity) -> Caller:
    """Log in through the real path: local JWT check, then tenant-mcp whoami."""
    return authenticate(create_token(identity.tenant_id, identity.user_email, identity.role))


@pytest.fixture(scope="session", autouse=True)
def tenant_mcp_server():
    """A real tenant-mcp server on a free port, sharing this process's SQLite DB and store."""
    from app.mcp_server.server import app

    stop = start_server(app, _mcp_port)
    yield MCP_URL
    stop()


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
    """New in-memory store, circuit breakers and graph health cache per test."""
    from app.harness import tenant_client

    get_store.cache_clear()
    _candidates.cache_clear()
    tenant_client._graph_health.update(checked=0.0, ok=False)
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
        self.tool_defs: list[list[Any]] = []
        self.model = FunctionModel(self._respond)

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.offered.append(sorted(t.name for t in info.function_tools))
        self.message_counts.append(len(messages))
        self.history.append(list(messages))
        self.tool_defs.append(list(info.function_tools))
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
