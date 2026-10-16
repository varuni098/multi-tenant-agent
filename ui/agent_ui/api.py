"""Client for the agent REST API, used by the UI backend. Tokens never reach the browser."""

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

API_URL = os.environ.get("AGENT_API_URL", "http://localhost:8000").rstrip("/")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        message = resp.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = resp.text[:200] or resp.reason_phrase
    raise ApiError(resp.status_code, message)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def get(token: str, path: str, params: dict[str, Any] | None = None) -> Any:
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as client:
        resp = await client.get(path, headers=_headers(token), params=clean)
    _raise_for(resp)
    return resp.json()


async def post(token: str, path: str, body: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as client:
        resp = await client.post(path, headers=_headers(token), json=body)
    _raise_for(resp)
    return resp.json()


@dataclass
class SSEParser:
    """Incremental Server-Sent Events parser: feed lines, get (event, data) when one completes."""

    event: str = "message"
    data: str = ""

    def feed(self, line: str) -> tuple[str, Any] | None:
        if line == "":
            if not self.data:
                self.event = "message"
                return None
            out = (self.event, json.loads(self.data))
            self.event, self.data = "message", ""
            return out
        if line.startswith(":"):  # comment / keep-alive
            return None
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            self.event = value
        elif field == "data":
            self.data = f"{self.data}\n{value}" if self.data else value
        return None


async def _stream(method: str, token: str, path: str, body: dict[str, Any] | None) -> AsyncIterator[tuple[str, Any]]:
    timeout = httpx.Timeout(connect=10, read=None, write=30, pool=10)  # streams stay open
    async with (
        httpx.AsyncClient(base_url=API_URL, timeout=timeout) as client,
        client.stream(method, path, headers=_headers(token), json=body) as resp,
    ):
        if resp.status_code >= 400:
            await resp.aread()
            _raise_for(resp)
        parser = SSEParser()
        async for line in resp.aiter_lines():
            event = parser.feed(line)
            if event:
                yield event


def stream_chat(token: str, prompt: str, session_id: str) -> AsyncIterator[tuple[str, Any]]:
    return _stream("POST", token, "/v1/chat/stream", {"prompt": prompt, "session_id": session_id})


def live_events(token: str) -> AsyncIterator[tuple[str, Any]]:
    return _stream("GET", token, "/v1/events", None)


# --- Development login ---


@dataclass(frozen=True)
class DevUser:
    email: str
    tenant_id: str
    tenant_name: str
    role: str

    @property
    def label(self) -> str:
        return f"{self.email} ({self.tenant_name}, {self.role})"


def dev_login_enabled() -> bool:
    return os.environ.get("APP_ENV", "dev") == "dev"


def dev_users() -> list[DevUser]:
    from app.db.seed import DEMO_TENANTS

    return [DevUser(email, t["id"], t["name"], role) for t in DEMO_TENANTS for email, role in t["users"].items()]


def dev_token(user: DevUser) -> str:
    """Mint a short-lived token for a seeded user. Only in APP_ENV=dev; production uses a real IdP."""
    if not dev_login_enabled():
        raise PermissionError("Development login is disabled.")
    from app.harness.auth import create_token

    return create_token(user.tenant_id, user.email, user.role, 120)
