import os
import tempfile

# Must be set before any app module reads settings
_db_file = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file}"
os.environ["WARMUP_ON_STARTUP"] = "false"

import pytest  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.seed import seed  # noqa: E402
from app.db.session import get_engine  # noqa: E402

TENANT_A = "a1b2c3d4-e5f6-7890-abcd-ef0123456789"
TENANT_B = "b2c3d4e5-f6a7-8901-bcde-f01234567890"


@pytest.fixture(autouse=True)
def db():
    """Fresh, seeded schema for every test."""
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    seed()
    yield
