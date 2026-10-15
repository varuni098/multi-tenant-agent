"""Input checks before the model runs, log redaction, and output claim checks."""

import re

from app.core.config import get_settings
from app.harness.context import Identity

UUID_PATTERN = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")

INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|rules?|prompt|polic(y|ies)|guardrails?)\b",
        r"\bsystem\s+prompt\b",
        r"\byou\s+are\s+now\b",
        r"\b(developer|god|admin|debug)\s+mode\b",
        r"\bjailbreak\b",
        r"\b(reveal|show|print|repeat)\b.{0,30}\b(instructions|prompt|secret|token|password)s?\b",
        r"\b(pretend|act)\s+(to\s+be|as)\b",
        r"\b(another|other|different|all)\s+tenants?\b",
    ]
]

# "has been changed", "I updated", "successfully set" — but not "is currently set to dark"
CLAIM_PATTERN = re.compile(
    r"\b(has|have|was|were|been|successfully|now|i)\s+(been\s+)?"
    r"(changed|updated|set|toggled|switched|reverted|undone|deleted|created|applied|removed)\b",
    re.IGNORECASE,
)

_REDACTIONS = [
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+\b"), "[token]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
    (re.compile(r"\b\d{9,}\b"), "[number]"),
]


def check_input(prompt: str, identity: Identity) -> str | None:
    """Return a refusal message if the prompt must not reach the model, else None."""
    text = prompt.strip()
    if not text:
        return "Please enter a request."
    if len(text) > get_settings().max_prompt_chars:
        return "That request is too long. Please shorten it."
    if any(u.lower() != identity.tenant_id.lower() for u in UUID_PATTERN.findall(text)):
        return "You can only access your own tenant's data."
    if any(p.search(text) for p in INJECTION_PATTERNS):
        return "I can't help with that request. I can read and change your pages' settings."
    return None


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def claims_change(answer: str) -> bool:
    return bool(CLAIM_PATTERN.search(answer))
