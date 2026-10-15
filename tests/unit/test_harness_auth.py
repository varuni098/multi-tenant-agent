from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.harness.auth import AuthError, authenticate, create_token
from tests.conftest import TENANT_A, TENANT_B


def _token(**overrides) -> str:
    claims = {
        "sub": "admin@acme.test",
        "tenant_id": TENANT_A,
        "role": "admin",
        "iss": "multi-tenant-agent",
        "aud": "multi-tenant-agent",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    claims.update(overrides)
    secret = claims.pop("_secret", Settings().jwt_secret)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, secret, algorithm="HS256")


def test_valid_token():
    identity = authenticate(create_token(TENANT_A, "admin@acme.test", "admin"))
    assert (identity.tenant_id, identity.user_email, identity.role) == (TENANT_A, "admin@acme.test", "admin")


def test_role_is_capped_by_database_role():
    # Token claims admin, but the DB says editor
    assert authenticate(create_token(TENANT_A, "editor@acme.test", "admin")).role == "editor"


def test_token_role_can_be_lower_than_database_role():
    assert authenticate(create_token(TENANT_A, "admin@acme.test", "viewer")).role == "viewer"


def test_user_must_belong_to_token_tenant():
    with pytest.raises(AuthError, match="not a member"):
        authenticate(create_token(TENANT_B, "admin@acme.test", "admin"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
        {"_secret": "some-other-secret-that-is-long-enough-123"},
        {"aud": "someone-else"},
        {"iss": "evil-issuer"},
        {"exp": None},
    ],
    ids=["expired", "wrong-secret", "wrong-audience", "wrong-issuer", "no-exp"],
)
def test_rejected_tokens(overrides):
    with pytest.raises(AuthError):
        authenticate(_token(**overrides))


def test_missing_tenant_claim():
    with pytest.raises(AuthError, match="tenant_id"):
        authenticate(_token(tenant_id=None))


def test_unknown_role_claim():
    with pytest.raises(AuthError):
        authenticate(_token(role="superuser"))


def test_garbage_token():
    with pytest.raises(AuthError):
        authenticate("not-a-jwt")


def test_no_local_tokens_in_prod(settings):
    settings.app_env = "prod"
    with pytest.raises(AuthError, match="disabled in production"):
        create_token(TENANT_A, "admin@acme.test", "admin")


def test_prod_refuses_default_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setenv("JWT_SECRET", "a-real-production-secret-of-good-length")
    assert Settings(_env_file=None).app_env == "prod"  # type: ignore[call-arg]
