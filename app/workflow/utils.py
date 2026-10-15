import re

from app.core.config import get_settings

UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def extract_uuid(text: str) -> str:
    """Extract a tenant UUID from the prompt, falling back to the configured default."""
    match = UUID_PATTERN.search(text)
    return match.group(0) if match else get_settings().default_tenant_uuid
