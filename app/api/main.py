"""Agent REST API, version 1.

Versioning: everything under /v1 only changes additively (new endpoints, new optional fields,
new event types). A breaking change ships as /v2 alongside /v1, and /v1 responses then carry
a Deprecation header for at least one release before removal.

Errors always look like {"error": {"code", "message", "request_id"}}.
"""

import asyncio
import contextvars
import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Any, Literal

import anyio
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.agent.warmup import ollama_ready, warm_up_model
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.harness import memory, tenant_client
from app.harness.auth import AuthError, authenticate
from app.harness.context import Caller
from app.harness.ratelimit import RateLimited, check_rate_limit
from app.harness.store import get_store
from app.harness.tenant_client import (
    ConfirmationError,
    TenantServiceUnavailable,
    confirm,
    graph_available,
    tenant_service_ready,
)
from app.live.bus import get_bus
from app.workflow.graph import ChatResult, run_workflow

setup_logging()
log = logging.getLogger(__name__)
settings = get_settings()

KEEPALIVE_SECONDS = 15
BUSY_RETRY_AFTER = "10"
LIVE_STREAM_MAX_SECONDS = 900  # clients reconnect (re-authenticating) at least this often


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if settings.warmup_on_startup:
        # Background thread: the API accepts requests while the model loads
        threading.Thread(target=warm_up_model, daemon=True).start()
    yield


app = FastAPI(
    title="Multi-Tenant Agent",
    version="0.7.0",
    description=__doc__,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "chat", "description": "Talk to the agent; confirm destructive actions."},
        {"name": "sessions", "description": "Conversation history of the calling user."},
        {"name": "tenant", "description": "Read-only views of the caller's tenant and live updates."},
        {"name": "ops", "description": "Liveness and readiness."},
    ],
)
bearer = HTTPBearer(auto_error=False)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = request_id[:64]
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


# --- Errors ---

ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "invalid_request",
    429: "rate_limited",
    503: "unavailable",
}


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorOut(BaseModel):
    error: ErrorBody


def _error(request: Request, status_code: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    body = ErrorOut(
        error=ErrorBody(
            code=ERROR_CODES.get(status_code, "error"),
            message=message,
            request_id=getattr(request.state, "request_id", ""),
        )
    )
    return JSONResponse(body.model_dump(), status_code=status_code, headers=headers)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    return _error(request, exc.status_code, str(exc.detail), dict(exc.headers) if exc.headers else None)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = ", ".join(".".join(str(p) for p in e["loc"] if p != "body") or "body" for e in exc.errors())
    return _error(request, status.HTTP_422_UNPROCESSABLE_CONTENT, f"Invalid request: {fields}.")


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorOut} for code in (401, 403, 404, 409, 422, 429, 503)
}


# --- Auth ---


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


def admin_caller(caller: Annotated[Caller, Depends(current_caller)]) -> Caller:
    if caller.identity.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can read the audit log.")
    return caller


# --- Admission control ---

# Each agent turn holds a slot for its whole run (including after a streaming client disconnects),
# so abandoned turns can't pile up behind the GPU. Load testing showed latency snowballing past
# 100 s without this.
run_slots = threading.BoundedSemaphore(settings.max_concurrent_runs)


def _busy() -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "The assistant is busy. Please retry shortly.",
        {"Retry-After": BUSY_RETRY_AFTER},
    )


@contextmanager
def run_slot() -> Any:
    if not run_slots.acquire(timeout=settings.run_queue_seconds):
        raise _busy()
    try:
        yield
    finally:
        run_slots.release()


# --- Schemas ---

SESSION_ID = r"^[A-Za-z0-9_-]{1,64}$"


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=settings.max_prompt_chars)
    session_id: str | None = Field(default=None, pattern=SESSION_ID)


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


class MeOut(BaseModel):
    tenant_id: str
    email: str
    role: str
    tenant_active: bool
    pages: list[str]


class TurnOut(BaseModel):
    prompt: str
    answer: str
    tools: list[str]


class SessionOut(BaseModel):
    session_id: str
    turns: list[TurnOut]


class SettingOut(BaseModel):
    key: str
    value: Any
    source: str


class PageSettingsOut(BaseModel):
    name: str
    settings: list[SettingOut]


class SettingsOut(BaseModel):
    defaults: list[SettingOut]
    pages: list[PageSettingsOut]


class AuditOut(BaseModel):
    kind: str
    items: list[dict[str, Any]]
    next_before_id: int | None


def _chat_response(result: ChatResult, session_id: str, request_id: str) -> ChatResponse:
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


def _sse(event: str, data: Any, event_id: str | None = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
SSE_RESPONSE: dict[int | str, dict[str, Any]] = {200: {"content": {"text/event-stream": {}}}}


# --- Ops ---


@app.get("/health", tags=["ops"])
def health() -> dict[str, Any]:
    """Liveness: the process is up."""
    return {"status": "ok", "model": settings.ollama_model}


def _redis_ready() -> bool:
    if not settings.redis_url:
        return True
    try:
        get_store().get("ready-check")
        return True
    except Exception:
        return False


@app.get("/ready", tags=["ops"])
def ready(response: Response) -> dict[str, bool]:
    """Readiness. `graph` is reported but optional: without it, writes fail closed (GRAPH_MODE=enforce)."""
    checks = {"ollama": ollama_ready(), "tenant_service": tenant_service_ready(), "redis": _redis_ready()}
    required_ok = all(checks.values())
    if settings.graph_mode != "off":
        checks["graph"] = graph_available()
    if not required_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return checks


# --- Chat ---


# Sync handlers: FastAPI runs them in worker threads, so agent.run_sync() is safe
@app.post("/v1/chat", response_model=ChatResponse, tags=["chat"], responses=ERROR_RESPONSES)
def chat(req: ChatRequest, request: Request, caller: Annotated[Caller, Depends(rate_limited_caller)]) -> ChatResponse:
    """One turn, answered when complete."""
    request_id = request.state.request_id
    session_id = req.session_id or uuid.uuid4().hex
    with run_slot():
        result = run_workflow(req.prompt, caller, request_id, session_id)
    return _chat_response(result, session_id, request_id)


@app.post("/v1/chat/stream", tags=["chat"], responses={**SSE_RESPONSE, **ERROR_RESPONSES})
async def chat_stream(
    req: ChatRequest, request: Request, caller: Annotated[Caller, Depends(rate_limited_caller)]
) -> StreamingResponse:
    """One turn as Server-Sent Events, in order:

    - `start` {request_id, session_id}
    - `intent` {intent}, `thinking` {tools_offered}
    - `tool_start` {tool, args} / `tool_end` {tool, outcome, detail?} for each tool call;
      outcome is ok | changed | denied | needs_confirmation | error
    - `result`: the same body as POST /v1/chat
    - `done`

    The answer arrives whole in `result`: the output validator checks every draft against what
    actually happened and may reject it, so draft tokens are never streamed. If the client
    disconnects, the turn still finishes; nothing is left half-applied.
    """
    request_id = request.state.request_id
    session_id = req.session_id or uuid.uuid4().hex
    if not await anyio.to_thread.run_sync(lambda: run_slots.acquire(timeout=settings.run_queue_seconds)):
        raise _busy()
    events: queue.Queue[tuple[str, Any] | None] = queue.Queue()

    def work() -> None:
        try:
            result = run_workflow(req.prompt, caller, request_id, session_id, emit=lambda t, d: events.put((t, d)))
            events.put(("result", _chat_response(result, session_id, request_id).model_dump()))
        except Exception:
            log.exception("[%s] streamed turn failed", request_id)
            events.put(("error", {"code": "internal", "message": "The turn failed.", "request_id": request_id}))
        finally:
            run_slots.release()
            events.put(None)

    # Carry context variables (e.g. model overrides, tracing) into the worker thread
    threading.Thread(target=contextvars.copy_context().run, args=(work,), daemon=True).start()

    async def stream() -> AsyncIterator[str]:
        yield _sse("start", {"request_id": request_id, "session_id": session_id})
        last = time.monotonic()
        while True:
            try:
                item = events.get_nowait()
            except queue.Empty:
                if time.monotonic() - last > KEEPALIVE_SECONDS:
                    last = time.monotonic()
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.05)
                continue
            if item is None:
                break
            last = time.monotonic()
            yield _sse(*item)
        yield _sse("done", {})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/v1/chat/confirm", response_model=ConfirmResponse, tags=["chat"], responses=ERROR_RESPONSES)
def confirm_action(
    req: ConfirmRequest, request: Request, caller: Annotated[Caller, Depends(rate_limited_caller)]
) -> ConfirmResponse:
    """Carry out a pending destructive action. Tokens are single use and bound to the user who asked."""
    try:
        message = confirm(caller.identity, req.token, request.state.request_id)
    except ConfirmationError as e:  # invalid/used token, other user, or the target changed meanwhile
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except TenantServiceUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
    return ConfirmResponse(message=message, request_id=request.state.request_id)


# --- Sessions ---


@app.get("/v1/sessions/{session_id}", response_model=SessionOut, tags=["sessions"], responses=ERROR_RESPONSES)
def get_session(
    caller: Annotated[Caller, Depends(current_caller)], session_id: Annotated[str, Path(pattern=SESSION_ID)]
) -> SessionOut:
    """The caller's own turns in a session. Other users' sessions are simply not found."""
    turns = memory.load_transcript(caller.identity, session_id)
    if not turns:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found or expired.")
    return SessionOut(session_id=session_id, turns=[TurnOut(**t) for t in turns])


@app.delete("/v1/sessions/{session_id}", status_code=204, tags=["sessions"], responses=ERROR_RESPONSES)
def delete_session(
    caller: Annotated[Caller, Depends(current_caller)], session_id: Annotated[str, Path(pattern=SESSION_ID)]
) -> Response:
    if not memory.delete_session(caller.identity, session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found or expired.")
    return Response(status_code=204)


# --- Tenant views ---


@app.get("/v1/me", response_model=MeOut, tags=["tenant"], responses=ERROR_RESPONSES)
def me(caller: Annotated[Caller, Depends(current_caller)]) -> MeOut:
    """Who the token belongs to, with the effective role (the lower of token and database role)."""
    i = caller.identity
    return MeOut(
        tenant_id=i.tenant_id,
        email=i.user_email,
        role=i.role,
        tenant_active=caller.tenant_active,
        pages=list(caller.page_names),
    )


def _tenant_call(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e
    except TenantServiceUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e


@app.get("/v1/settings", response_model=SettingsOut, tags=["tenant"], responses=ERROR_RESPONSES)
def tenant_settings(request: Request, caller: Annotated[Caller, Depends(current_caller)]) -> SettingsOut:
    """Effective settings of every page and the tenant defaults, with where each value comes from."""
    view = _tenant_call(lambda: tenant_client.settings_view(caller.identity, request.state.request_id))
    return SettingsOut(**view)


@app.get("/v1/audit", response_model=AuditOut, tags=["tenant"], responses=ERROR_RESPONSES)
def audit(
    request: Request,
    caller: Annotated[Caller, Depends(admin_caller)],
    kind: Literal["changes", "tool_calls"] = "changes",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_id: Annotated[int | None, Query(ge=1, description="Page backwards: pass next_before_id")] = None,
    actor: Annotated[str | None, Query(max_length=255)] = None,
    page_name: Annotated[str | None, Query(max_length=100, description="changes only")] = None,
) -> AuditOut:
    """Admin only: setting changes or tool calls in the caller's tenant, newest first."""
    query = {"kind": kind, "limit": limit, "before_id": before_id, "actor": actor, "page_name": page_name}
    items = _tenant_call(lambda: tenant_client.audit(caller.identity, request.state.request_id, query))
    next_before = items[-1]["id"] if len(items) == limit else None
    return AuditOut(kind=kind, items=items, next_before_id=next_before)


@app.get("/v1/events", tags=["tenant"], responses={**SSE_RESPONSE, **ERROR_RESPONSES})
async def live_events(request: Request, caller: Annotated[Caller, Depends(current_caller)]) -> StreamingResponse:
    """Live tenant changes as Server-Sent Events, whoever made them (agent, MCP client, admin).

    Events: `ready`, then `setting.changed` | `page.created` | `page.deleted` |
    `tenant.template_changed` with {page_name, key, after, actor, at}. Re-read /v1/settings
    for the full picture. The stream ends after 15 minutes; reconnect to continue.
    """
    tenant_id = caller.identity.tenant_id

    async def stream() -> AsyncIterator[str]:
        with get_bus().subscribe(tenant_id) as sub:
            yield _sse("ready", {"tenant_id": tenant_id})
            started = last = time.monotonic()
            while time.monotonic() - started < LIVE_STREAM_MAX_SECONDS:
                if await request.is_disconnected():
                    return
                event = sub.get(timeout=0)
                if event is None:
                    if time.monotonic() - last > KEEPALIVE_SECONDS:
                        last = time.monotonic()
                        yield ": keep-alive\n\n"
                    await asyncio.sleep(0.25)
                    continue
                last = time.monotonic()
                yield _sse(str(event.get("type") or "change"), event, str(event.get("event_id") or ""))

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)
