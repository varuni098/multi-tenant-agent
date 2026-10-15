from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel

from app.agent.agent import agent
from app.api.main import app
from tests.conftest import TENANT_A

client = TestClient(app)


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_chat():
    with agent.override(model=TestModel(custom_output_text="login is light")):
        resp = client.post("/chat", json={"prompt": f"theme of login for {TENANT_A}?"})
    assert resp.status_code == 200
    assert resp.json() == {"tenant_uuid": TENANT_A, "agent_response": "login is light"}


def test_chat_rejects_missing_prompt():
    assert client.post("/chat", json={}).status_code == 422
