import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from pydantic import BaseModel

from app.agent.warmup import ollama_ready, warm_up_model
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.repository import ping
from app.workflow.graph import run_workflow
from app.workflow.utils import extract_uuid

setup_logging()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if get_settings().warmup_on_startup:
        # Background thread: the API accepts requests while the model loads
        threading.Thread(target=warm_up_model, daemon=True).start()
    yield


app = FastAPI(title="Multi-Tenant Agent", lifespan=lifespan)


class ChatRequest(BaseModel):
    prompt: str


class ChatResponse(BaseModel):
    tenant_uuid: str
    agent_response: str


@app.get("/health")
def health():
    """Liveness: the process is up."""
    return {"status": "ok", "model": get_settings().ollama_model}


@app.get("/ready")
def ready(response: Response):
    """Readiness: dependencies are reachable."""
    checks = {"ollama": ollama_ready()}
    try:
        ping()
        checks["database"] = True
    except Exception:
        checks["database"] = False
    if not all(checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return checks


# Sync handler: FastAPI runs it in a worker thread, so agent.run_sync() is safe here
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    final_state = run_workflow(req.prompt, extract_uuid(req.prompt))
    return ChatResponse(tenant_uuid=final_state["tenant_uuid"], agent_response=final_state["agent_response"])
