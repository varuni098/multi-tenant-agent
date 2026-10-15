from fastapi.testclient import TestClient

from app.api.main import app
from app.harness.auth import create_token
from tests.conftest import TENANT_A

client = TestClient(app)


def _auth(email="admin@acme.test", role="admin", tenant=TENANT_A):
    return {"Authorization": f"Bearer {create_token(tenant, email, role)}"}


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_chat_requires_token():
    resp = client.post("/v1/chat", json={"prompt": "hi"})
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_chat_rejects_bad_token():
    resp = client.post("/v1/chat", json={"prompt": "hi"}, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_old_unauthenticated_endpoint_is_gone():
    assert client.post("/chat", json={"prompt": "hi"}).status_code == 404


def test_chat(script):
    script([("set_theme", {"page_name": "login", "theme": "dark"})], "Done.")
    resp = client.post(
        "/v1/chat",
        json={"prompt": "make login dark", "session_id": "abc"},
        headers={**_auth("editor@acme.test", "editor"), "X-Request-ID": "req-42"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Done."
    assert body["actions"] == [{"tool": "set_theme", "message": "Changed 'theme' from 'light' to 'dark'."}]
    assert (body["requires_confirmation"], body["session_id"], body["request_id"]) == (False, "abc", "req-42")
    assert resp.headers["X-Request-ID"] == "req-42"


def test_confirmation_flow(script):
    script([("delete_page", {"page_name": "settings"})], "ok")
    body = client.post("/v1/chat", json={"prompt": "delete the settings page"}, headers=_auth()).json()
    assert body["requires_confirmation"] is True
    token = body["confirmation"]["token"]

    resp = client.post("/v1/chat/confirm", json={"token": token}, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["message"] == "Deleted page 'settings'."
    # Single use
    assert client.post("/v1/chat/confirm", json={"token": token}, headers=_auth()).status_code == 409


def test_confirm_by_other_user_is_rejected(script):
    script([("delete_page", {"page_name": "settings"})], "ok")
    token = client.post("/v1/chat", json={"prompt": "delete settings page"}, headers=_auth()).json()["confirmation"][
        "token"
    ]
    resp = client.post("/v1/chat/confirm", json={"token": token}, headers=_auth("editor@acme.test", "editor"))
    assert resp.status_code == 409


def test_rate_limit(script, settings):
    settings.rate_limit_user_per_minute = 1
    script("a", "b")
    headers = _auth()
    assert client.post("/v1/chat", json={"prompt": "theme of login?"}, headers=headers).status_code == 200
    resp = client.post("/v1/chat", json={"prompt": "theme of login?"}, headers=headers)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "60"


def test_prompt_validation():
    assert client.post("/v1/chat", json={"prompt": ""}, headers=_auth()).status_code == 422
    assert client.post("/v1/chat", json={"prompt": "x" * 5000}, headers=_auth()).status_code == 422
    assert client.post("/v1/chat", json={"prompt": "hi", "session_id": "../etc"}, headers=_auth()).status_code == 422
