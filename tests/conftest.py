import os
import tempfile

# Must be set before any app module reads settings
_db_file = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file}"
os.environ["WARMUP_ON_STARTUP"] = "false"

import pytest  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.seed import TENANT_A, TENANT_B, seed  # noqa: E402
from app.db.session import get_engine  # noqa: E402

__all__ = ["TENANT_A", "TENANT_B"]


@pytest.fixture(autouse=True)
def db():
    """Fresh, seeded schema for every test.

    TENANT_A (Acme, free plan): login=light, dashboard=dark, settings=inherited (light from template)
    TENANT_B (Globex, pro plan): login=dark
    """
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    seed()
    yield
