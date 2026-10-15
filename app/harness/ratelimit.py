import time

from app.core.config import get_settings
from app.harness.context import Identity
from app.harness.store import get_store


class RateLimited(Exception):
    pass


def check_rate_limit(identity: Identity) -> None:
    """Fixed one-minute windows per user and per tenant."""
    settings = get_settings()
    window = int(time.time() // 60)
    store = get_store()
    user_count = store.incr(f"rl:user:{identity.tenant_id}:{identity.user_email}:{window}", 60)
    tenant_count = store.incr(f"rl:tenant:{identity.tenant_id}:{window}", 60)
    if user_count > settings.rate_limit_user_per_minute or tenant_count > settings.rate_limit_tenant_per_minute:
        raise RateLimited("Too many requests. Please wait a minute and try again.")
