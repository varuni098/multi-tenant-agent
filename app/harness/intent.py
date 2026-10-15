"""Cheap keyword intent classification (no model call).

Erring towards "write" is safe: it only exposes write tools the role already allows.
"""

import re

from app.harness.policy import Intent

WRITE_WORDS = {
    "set", "change", "toggle", "switch", "flip", "make", "turn", "update", "undo", "revert", "restore",
    "enable", "disable", "create", "add", "delete", "remove", "apply", "reset", "use", "put", "rename",
}  # fmt: skip
DOMAIN_WORDS = {
    "theme", "themes", "dark", "light", "mode", "page", "pages", "setting", "settings", "language",
    "layout", "compact", "comfortable", "brand", "color", "colour", "tips", "template", "templates",
    "history", "change", "changes", "changed", "undo", "revert", "screen", "screens",
    "branding", "plan", "plans", "feature", "features", "role", "roles", "permission", "permissions",
    "confirm", "confirmation", "inherit", "inheritance", "upgrade", "upgrading", "upgraded", "docs", "documentation",
    "tenant", "tenants", "editor", "viewer", "admin", "locale",
}  # fmt: skip

OFF_TOPIC_REPLY = (
    "I can help with your pages' settings: theme, language, layout, brand colour and tips. "
    "For example: 'What theme is the login page?' or 'Set the dashboard to dark'."
)


def classify(prompt: str, page_names: list[str]) -> Intent:
    words = set(re.findall(r"[a-z_]+", prompt.lower()))
    domain = DOMAIN_WORDS | {w for name in page_names for w in re.findall(r"[a-z_]+", name.lower())}
    if words & WRITE_WORDS:
        return "write"
    if words & domain:
        return "read"
    return "off_topic"
