import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.agent.warmup import ollama_ready, warm_up_model
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.harness.auth import AuthError, authenticate
from app.harness.context import Caller
from app.harness.ratelimit import RateLimited, check_rate_limit
from app.harness.tenant_client import ConfirmationError, TenantServiceUnavailable, confirm, tenant_service_ready
from app.workflow.graph import run_workflow

setup_logging()
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if settings.warmup_on_startup:
        # Background thread: the API accepts requests while the model loads
        threading.Thread(target=warm_up_model, daemon=True).start()
    yield


app = FastAPI(title="Multi-Tenant Agent", version="0.6.0", lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = request_id[:64]
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


def current_caller(request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Caller:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token", {"WWW-Authenticate": "Bearer"})
    try:
        return authenticate(creds.credentials, request.state.request_id)
    except AuthError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e), {"WWW-Authenticate": "Bearer"}) from e
    except TenantServiceUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e


def rate_limited_caller(caller: Annotated[Caller, Depends(current_caller)]) -> Caller:
    try:
        check_rate_limit(caller.identity)
    except RateLimited as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(e), {"Retry-After": "60"}) from e
    return caller


# --- Schemas ---


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=settings.max_prompt_chars)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class ActionOut(BaseModel):
    tool: str
    message: str


class DenialOut(BaseModel):
    tool: str
    rule: str
    reason: str


class CitationOut(BaseModel):
    rank: int
    title: str
    heading: str
    path: str


class ConfirmationOut(BaseModel):
    token: str
    summary: str
    expires_in: int


class ChatResponse(BaseModel):
    answer: str
    intent: str | None
    blocked: bool
    actions: list[ActionOut]
    denials: list[DenialOut]
    citations: list[CitationOut]
    requires_confirmation: bool
    confirmation: ConfirmationOut | None
    model: str | None
    session_id: str
    request_id: str


class ConfirmRequest(BaseModel):
    token: str = Field(min_length=8, max_length=64)


class ConfirmResponse(BaseModel):
    message: str
    request_id: str


# --- Routes ---


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness: the process is up."""
    return {"status": "ok", "model": settings.ollama_model}


@app.get("/ready")
def ready(response: Response) -> dict[str, bool]:
    """Readiness: dependencies are reachable."""
    checks = {"ollama": ollama_ready(), "tenant_service": tenant_service_ready()}
    if not all(checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return checks


# Sync handlers: FastAPI runs them in worker threads, so agent.run_sync() is safe
@app.post("/v1/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request, caller: Annotated[Caller, Depends(rate_limited_caller)]) -> ChatResponse:
    request_id = request.state.request_id
    session_id = req.session_id or uuid.uuid4().hex
    result = run_workflow(req.prompt, caller, request_id, session_id)
    return ChatResponse(
        answer=result.answer,
        intent=result.intent,
        blocked=result.blocked,
        actions=[ActionOut(tool=a.tool, message=a.message) for a in result.actions],
        denials=[DenialOut(tool=d.tool, rule=d.rule, reason=d.reason) for d in result.denials],
        citations=[CitationOut(**vars(c)) for c in result.citations],
        requires_confirmation=result.confirmation is not None,
        confirmation=ConfirmationOut(**vars(result.confirmation)) if result.confirmation else None,
        model=result.model,
        session_id=session_id,
        request_id=request_id,
    )


@app.post("/v1/chat/confirm", response_model=ConfirmResponse)
def confirm_action(
    req: ConfirmRequest, request: Request, caller: Annotated[Caller, Depends(rate_limited_caller)]
) -> ConfirmResponse:
    try:
        message = confirm(caller.identity, req.token, request.state.request_id)
    except ConfirmationError as e:  # invalid/used token, other user, or the target changed meanwhile
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except TenantServiceUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
    return ConfirmResponse(message=message, request_id=request.state.request_id)
