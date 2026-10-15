import logging

import httpx

from app.core.config import get_settings

log = logging.getLogger(__name__)


def warm_up_model() -> None:
    """Load the model into GPU memory so the first user request isn't slow."""
    settings = get_settings()
    try:
        # An empty prompt makes Ollama load the model without generating anything
        httpx.post(
            f"{settings.ollama_native_url}/api/generate",
            json={"model": settings.ollama_model, "prompt": "", "keep_alive": settings.ollama_keep_alive},
            timeout=300,
        ).raise_for_status()
        log.info("Model %s warmed up", settings.ollama_model)
    except httpx.HTTPError as e:
        log.warning("Model warm-up failed: %s", e)


def ollama_ready() -> bool:
    try:
        httpx.get(f"{get_settings().ollama_native_url}/api/tags", timeout=3).raise_for_status()
        return True
    except httpx.HTTPError:
        return False
