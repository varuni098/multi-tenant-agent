"""LangGraph request flow:

check_tenant -> guard_input -> classify -> run_agent | off_topic
            \-> blocked (any failed check)
"""

import logging
from dataclasses import dataclass, field
from typing import TypedDict

from langgraph.graph import END, StateGraph
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded

from app.harness import memory
from app.harness.context import Action, Identity, PendingConfirmation, RunDeps
from app.harness.guardrails import check_input, redact
from app.harness.intent import OFF_TOPIC_REPLY, classify
from app.harness.policy import Intent, allowed_tools
from app.harness.reliability import ModelUnavailable, run_agent
from app.services.settings import list_pages
from app.services.tenants import is_tenant_active

log = logging.getLogger(__name__)

SAFE_FAILURE_REPLY = "I wasn't able to complete that reliably, so nothing was changed. Please rephrase."


@dataclass
class ChatResult:
    answer: str
    intent: Intent | None = None
    blocked: bool = False
    actions: list[Action] = field(default_factory=list)
    confirmation: PendingConfirmation | None = None
    model: str | None = None


class State(TypedDict):
    prompt: str
    identity: Identity
    request_id: str
    session_id: str
    intent: Intent | None
    result: ChatResult | None


def check_tenant_node(state: State) -> State:
    if not is_tenant_active(state["identity"].tenant_id):
        log.info("[%s] blocked: tenant inactive", state["request_id"])
        state["result"] = ChatResult(answer="Operation blocked: Invalid or inactive tenant.", blocked=True)
    return state


def guard_input_node(state: State) -> State:
    refusal = check_input(state["prompt"], state["identity"])
    if refusal:
        log.warning("[%s] blocked by guardrail: %s", state["request_id"], redact(state["prompt"])[:200])
        state["result"] = ChatResult(answer=refusal, blocked=True)
    return state


def classify_node(state: State) -> State:
    pages = [p.name for p in list_pages(state["identity"].tenant_id)]
    state["intent"] = classify(state["prompt"], pages)
    log.info("[%s] intent=%s", state["request_id"], state["intent"])
    return state


def off_topic_node(state: State) -> State:
    state["result"] = ChatResult(answer=OFF_TOPIC_REPLY, intent="off_topic")
    return state


def run_agent_node(state: State) -> State:
    identity, intent = state["identity"], state["intent"] or "write"
    deps = RunDeps(
        identity=identity,
        request_id=state["request_id"],
        allowed_tools=allowed_tools(identity.role, intent),
    )
    history = memory.load_history(identity, state["session_id"])
    try:
        run, model = run_agent(state["prompt"], deps, history)
        answer = run.output
        memory.append_turn(identity, state["session_id"], run.new_messages_json())
    except (UnexpectedModelBehavior, UsageLimitExceeded) as e:
        log.warning("[%s] agent gave up: %s", state["request_id"], e)
        answer, model = SAFE_FAILURE_REPLY, None
    except ModelUnavailable as e:
        answer, model = str(e), None

    confirmation = deps.confirmations[-1] if deps.confirmations else None
    if confirmation:
        # Deterministic wording: the model must not paraphrase a pending action as done
        answer = f"Please confirm: {confirmation.summary}"
    elif deps.verification_failed and not deps.actions:
        answer = SAFE_FAILURE_REPLY
    state["result"] = ChatResult(
        answer=answer, intent=intent, actions=deps.actions, confirmation=confirmation, model=model
    )
    return state


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


def run_workflow(prompt: str, identity: Identity, request_id: str, session_id: str) -> ChatResult:
    state: State = {
        "prompt": prompt,
        "identity": identity,
        "request_id": request_id,
        "session_id": session_id,
        "intent": None,
        "result": None,
    }
    final = app_graph.invoke(state)
    result = final["result"]
    assert result is not None
    return result
