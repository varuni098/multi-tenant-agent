"""Cheap keyword intent classification (no model call).

Erring towards "write" is safe: it only exposes write tools the role already allows. So a
prompt is a read only when it is clearly a question or a show/list request; requests phrased
any other way ("I'd like the login page dark", "get rid of the dashboard") count as writes.
"""

import re

from app.harness.policy import Intent

WRITE_WORDS = {
    "set", "change", "toggle", "switch", "flip", "make", "turn", "update", "undo", "revert", "restore",
    "enable", "disable", "create", "add", "delete", "remove", "apply", "reset", "use", "put", "rename",
}  # fmt: skip
# How a read starts: a question or a request to show something. "Could/can/would you ..." is a
# polite request, so it is not here.
READ_STARTS = {
    "what", "which", "how", "is", "are", "does", "do", "did", "why", "who", "when", "where", "was", "were",
    "show", "list", "tell", "explain", "describe", "display", "give", "get", "check",
}  # fmt: skip
DOMAIN_WORDS = {
    "theme", "themes", "dark", "light", "mode", "page", "pages", "setting", "settings", "language",
    "layout", "compact", "comfortable", "brand", "color", "colour", "tips", "template", "templates",
    "history", "change", "changes", "changed", "undo", "revert", "screen", "screens",
    "branding", "plan", "plans", "feature", "features", "role", "roles", "permission", "permissions",
    "confirm", "confirmation", "inherit", "inheritance", "upgrade", "upgrading", "upgraded", "docs", "documentation",
    "tenant", "tenants", "editor", "viewer", "admin", "locale", "hint", "hints",
}  # fmt: skip

OFF_TOPIC_REPLY = (
    "I can help with your pages' settings: theme, language, layout, brand colour and tips. "
    "For example: 'What theme is the login page?' or 'Set the dashboard to dark'."
)


def classify(prompt: str, page_names: list[str]) -> Intent:
    tokens = re.findall(r"[a-z_]+", prompt.lower())
    words = set(tokens)
    domain = DOMAIN_WORDS | {w for name in page_names for w in re.findall(r"[a-z_]+", name.lower())}
    if words & WRITE_WORDS:
        return "write"
    if not words & domain:
        return "off_topic"
    if tokens and tokens[0] in READ_STARTS and not _is_request(tokens):
        return "read"
    return "write"


def _is_request(tokens: list[str]) -> bool:
    """'get rid of ...' starts like a read but asks for a change."""
    return tokens[:2] == ["get", "rid"]
