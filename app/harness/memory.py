"""Conversation history per tenant + user + session, trimmed to whole turns."""

import json
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)

from app.core.config import get_settings
from app.harness.context import Identity
from app.harness.store import get_store


def _key(identity: Identity, session_id: str) -> str:
    return f"hist:{identity.tenant_id}:{identity.user_email}:{session_id}"


def load_history(identity: Identity, session_id: str) -> list[ModelMessage]:
    raw = get_store().get(_key(identity, session_id))
    if not raw:
        return []
    messages: list[ModelMessage] = []
    for turn in json.loads(raw):
        messages.extend(ModelMessagesTypeAdapter.validate_json(turn))
    return messages


def append_turn(identity: Identity, session_id: str, new_messages_json: bytes) -> None:
    """Store one run's messages. Trimming drops whole turns so tool calls never lose their results."""
    settings = get_settings()
    store = get_store()
    key = _key(identity, session_id)
    turns: list[str] = json.loads(store.get(key) or "[]")
    turns.append(new_messages_json.decode())
    store.set(key, json.dumps(turns[-settings.session_max_turns :]), settings.session_ttl_seconds)


def load_transcript(identity: Identity, session_id: str) -> list[dict[str, Any]]:
    """The session as turns {prompt, answer, tools} for display. Empty if unknown or expired."""
    raw = get_store().get(_key(identity, session_id))
    turns = []
    for turn in json.loads(raw) if raw else []:
        prompt, answer, tools = "", "", []
        for message in ModelMessagesTypeAdapter.validate_json(turn):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str) and not prompt:
                    prompt = part.content
                elif isinstance(part, ToolCallPart):
                    tools.append(part.tool_name)
            if isinstance(message, ModelResponse):
                text = "".join(p.content for p in message.parts if isinstance(p, TextPart))
                answer = text or answer  # the last text response is the one the user saw
        turns.append({"prompt": prompt, "answer": answer.strip(), "tools": tools})
    return turns


def delete_session(identity: Identity, session_id: str) -> bool:
    return get_store().pop(_key(identity, session_id)) is not None
