r"""LangGraph request flow:

check_tenant -> guard_input -> classify -> run_agent | off_topic
            \-> blocked (any failed check)
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NotRequired, TypedDict

from langgraph.graph import END, StateGraph
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded

from app.harness import memory
from app.harness.context import Action, Caller, Citation, Denial, PendingConfirmation, RunDeps
from app.harness.guardrails import check_input, redact
from app.harness.intent import OFF_TOPIC_REPLY, classify
from app.harness.policy import TOOL_POLICIES, Intent, allowed_tools
from app.harness.reliability import ModelUnavailable, run_agent
from app.harness.tenant_client import graph_available

log = logging.getLogger(__name__)

SAFE_FAILURE_REPLY = "I wasn't able to complete that reliably, so nothing was changed. Please rephrase."
NO_PERMISSION_REPLY = "You don't have permission to change settings, so nothing was changed."


@dataclass
class ChatResult:
    answer: str
    intent: Intent | None = None
    blocked: bool = False
    actions: list[Action] = field(default_factory=list)
    denials: list[Denial] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    confirmation: PendingConfirmation | None = None
    model: str | None = None
    tokens: int | None = None  # input + output tokens across the run's model requests
    model_requests: int | None = None


class State(TypedDict):
    prompt: str
    caller: Caller
    request_id: str
    session_id: str
    intent: Intent | None
    result: ChatResult | None
    emit: NotRequired[Callable[[str, dict[str, Any]], None] | None]


def _emit(state: State, event_type: str, **data: Any) -> None:
    emit = state.get("emit")
    if emit is not None:
        emit(event_type, data)


def check_tenant_node(state: State) -> State:
    if not state["caller"].tenant_active:
        log.info("[%s] blocked: tenant inactive", state["request_id"])
        state["result"] = ChatResult(answer="Operation blocked: Invalid or inactive tenant.", blocked=True)
    return state


def guard_input_node(state: State) -> State:
    refusal = check_input(state["prompt"], state["caller"].identity)
    if refusal:
        log.warning("[%s] blocked by guardrail: %s", state["request_id"], redact(state["prompt"])[:200])
        state["result"] = ChatResult(answer=refusal, blocked=True)
    return state


def classify_node(state: State) -> State:
    state["intent"] = classify(state["prompt"], list(state["caller"].page_names))
    log.info("[%s] intent=%s", state["request_id"], state["intent"])
    _emit(state, "intent", intent=state["intent"])
    return state


def off_topic_node(state: State) -> State:
    state["result"] = ChatResult(answer=OFF_TOPIC_REPLY, intent="off_topic")
    return state


def run_agent_node(state: State) -> State:
    identity, intent = state["caller"].identity, state["intent"] or "write"
    deps = RunDeps(
        identity=identity,
        request_id=state["request_id"],
        allowed_tools=allowed_tools(identity.role, intent, graph=graph_available()),
        emit=state.get("emit"),
    )
    _emit(state, "thinking", tools_offered=len(deps.allowed_tools))
    # A write request from a role with no write tools fails as a permission issue, not "rephrase"
    can_write = any(TOOL_POLICIES[t].writes for t in deps.allowed_tools)
    failure_reply = SAFE_FAILURE_REPLY if can_write or intent == "read" else NO_PERMISSION_REPLY
    history = memory.load_history(identity, state["session_id"])
    tokens: int | None = None
    requests: int | None = None
    try:
        run, model = run_agent(state["prompt"], deps, history)
        answer = run.output
        usage = run.usage
        tokens = (usage.input_tokens or 0) + (usage.output_tokens or 0)
        requests = usage.requests
        memory.append_turn(identity, state["session_id"], run.new_messages_json())
    except (UnexpectedModelBehavior, UsageLimitExceeded) as e:
        # e.g. the model kept calling a tool it isn't offered, or looped past the limits
        log.warning("[%s] agent gave up: %s", state["request_id"], e)
        answer, model = failure_reply, None
    except ModelUnavailable as e:
        answer, model = str(e), None
    except Exception:
        # e.g. tenant-mcp unreachable mid-run; never surface internals to the user
        log.exception("[%s] agent run failed", state["request_id"])
        answer, model = "The assistant is temporarily unavailable. Please try again shortly.", None

    confirmation = deps.confirmations[-1] if deps.confirmations else None
    if confirmation:
        # Deterministic wording: the model must not paraphrase a pending action as done
        answer = f"Please confirm: {confirmation.summary}"
    elif deps.verification_failed and not deps.actions:
        answer = failure_reply
    state["result"] = ChatResult(
        answer=answer,
        intent=intent,
        actions=deps.actions,
        denials=deps.denials,
        citations=_unique(deps.citations),
        confirmation=confirmation,
        model=model,
        tokens=tokens,
        model_requests=requests,
    )
    return state


def _unique(citations: list[Citation]) -> list[Citation]:
    """Keep the first citation per source section (search_docs may run more than once)."""
    seen: set[tuple[str, str]] = set()
    out = []
    for c in citations:
        if (c.path, c.heading) not in seen:
            seen.add((c.path, c.heading))
            out.append(c)
    return out


def _after(next_node: str):
    def route(state: State) -> str:
        return "end" if state["result"] is not None else next_node

    return route


graph = StateGraph(State)
graph.add_node("check_tenant", check_tenant_node)
graph.add_node("guard_input", guard_input_node)
graph.add_node("classify", classify_node)
graph.add_node("run_agent", run_agent_node)
graph.add_node("off_topic", off_topic_node)
graph.set_entry_point("check_tenant")
graph.add_conditional_edges("check_tenant", _after("guard_input"), {"guard_input": "guard_input", "end": END})
graph.add_conditional_edges("guard_input", _after("classify"), {"classify": "classify", "end": END})
graph.add_conditional_edges(
    "classify",
    lambda s: "off_topic" if s["intent"] == "off_topic" else "run_agent",
    {"off_topic": "off_topic", "run_agent": "run_agent"},
)
graph.add_edge("run_agent", END)
graph.add_edge("off_topic", END)

app_graph = graph.compile()


def run_workflow(
    prompt: str,
    caller: Caller,
    request_id: str,
    session_id: str,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> ChatResult:
    """Run one turn. `emit` receives progress events (intent, tool calls) for streaming clients."""
    state: State = {
        "prompt": prompt,
        "caller": caller,
        "request_id": request_id,
        "session_id": session_id,
        "intent": None,
        "result": None,
        "emit": emit,
    }
    final = app_graph.invoke(state)
    result = final["result"]
    assert result is not None
    return result
