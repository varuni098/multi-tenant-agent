import os
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel
from run_workflow import extract_uuid
from tenant_workflow import app_graph

load_dotenv()

app = FastAPI(title="Multi-Tenant Agent")

class ChatRequest(BaseModel):
    prompt: str

class ChatResponse(BaseModel):
    tenant_uuid: str
    agent_response: str

@app.get("/health")
def health():
    return {"status": "ok", "model": os.getenv("OLLAMA_MODEL", "qwen3:8b")}

# Sync handler: FastAPI runs it in a worker thread, so agent.run_sync() is safe here
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    initial_state = {
        "prompt": req.prompt,
        "tenant_uuid": extract_uuid(req.prompt),
        "is_valid": False,
        "agent_response": ""
    }
    final_state = app_graph.invoke(initial_state)
    return ChatResponse(tenant_uuid=final_state["tenant_uuid"], agent_response=final_state["agent_response"])
