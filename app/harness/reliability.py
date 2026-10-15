"""Model selection with timeouts, usage limits, a circuit breaker and an optional fallback model."""

import logging
import threading
import time
from dataclasses import dataclass
from functools import lru_cache

from pydantic_ai.agent import AgentRunResult
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.usage import UsageLimits

from app.agent.agent import agent
from app.core.config import get_settings
from app.harness.context import RunDeps
from app.harness.tenant_client import build_toolset

log = logging.getLogger(__name__)

# Model-side failures only; tenant-mcp failures are handled by the workflow
_TRANSIENT = (ModelAPIError, TimeoutError)


class ModelUnavailable(Exception):
    pass


class CircuitBreaker:
    """Opens after N consecutive failures; lets one trial call through after the cooldown."""

    def __init__(self, threshold: int, cooldown: float) -> None:
        self.threshold, self.cooldown = threshold, cooldown
        self.failures = 0
        self.opened_at: float | None = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            return self.opened_at is None or time.monotonic() - self.opened_at >= self.cooldown

    def record_success(self) -> None:
        with self._lock:
            self.failures, self.opened_at = 0, None

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.monotonic()


@dataclass
class _Candidate:
    name: str
    model: OpenAIChatModel
    breaker: CircuitBreaker


def build_model(name: str) -> OpenAIChatModel:
    return OpenAIChatModel(model_name=name, provider=OllamaProvider(base_url=get_settings().ollama_base_url))


@lru_cache
def _candidates() -> list[_Candidate]:
    s = get_settings()
    names = [s.ollama_model] + ([s.ollama_fallback_model] if s.ollama_fallback_model else [])
    return [
        _Candidate(n, build_model(n), CircuitBreaker(s.breaker_failure_threshold, s.breaker_cooldown_seconds))
        for n in names
    ]


def run_agent(prompt: str, deps: RunDeps, history: list[ModelMessage]) -> tuple[AgentRunResult[str], str]:
    """Run the agent on the first healthy model. Returns (result, model name)."""
    s = get_settings()
    limits = UsageLimits(request_limit=s.max_requests_per_run, tool_calls_limit=s.max_tool_calls_per_run)
    for candidate in _candidates():
        if not candidate.breaker.allow():
            log.warning("[%s] skipping %s: circuit open", deps.request_id, candidate.name)
            continue
        try:
            result = agent.run_sync(
                prompt,
                deps=deps,
                model=candidate.model,
                message_history=history,
                toolsets=[build_toolset(deps)],
                usage_limits=limits,
                model_settings={"timeout": s.llm_timeout_seconds},
            )
        except _TRANSIENT as e:
            candidate.breaker.record_failure()
            log.warning("[%s] model %s failed: %s", deps.request_id, candidate.name, e)
            continue
        candidate.breaker.record_success()
        return result, candidate.name
    raise ModelUnavailable("The assistant is temporarily unavailable. Please try again shortly.")
