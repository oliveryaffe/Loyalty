"""Shared pytest fixtures.

Two DB tracks are provided:

- `db_session` / `client`: an isolated in-memory SQLite DB, fresh per test
  -- for ledger math and API smoke tests that want a clean slate.
- `seeded_db`: the shared on-disk SQLite DB populated once per test session
  by the real `scripts/seed_data.py` generator (same code path used for
  local dev) -- for AI-layer tests that need realistic volume and the
  known synthetic cohorts / injected fraud labels.
"""
import os
import sys
import tempfile
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Must be set before any `app.*` module is imported anywhere (including by
# this file, below) since pydantic-settings reads env vars at import time.
#
# NOTE: the test DB file is deliberately placed under the OS temp dir
# (/tmp) rather than next to the project files. Some sandboxed/networked
# filesystems (e.g. the dev-container bind mount this was built under)
# don't support SQLite's file-locking semantics and raise
# `sqlite3.OperationalError: disk I/O error` when the project directory
# itself lives on such a mount. /tmp is a local filesystem and doesn't
# have this problem. This does not affect normal local/native use of the
# app (see README) -- only where the *repo* happens to live on such a mount.
_TEST_DB_DIR = Path("/tmp") / f"loyalty_ai_framework_test_{uuid.uuid4().hex[:8]}"
_TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
_TEST_DB_PATH = _TEST_DB_DIR / "test_loyalty.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH.as_posix()}"
os.environ["JWT_SECRET_KEY"] = "test-only-secret"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db.base import Base, SessionLocal, get_db  # noqa: E402
from app.main import app  # noqa: E402
from scripts.seed_data import DEMO_MERCHANT_EMAIL, DEMO_MERCHANT_PASSWORD  # noqa: E402
from scripts.seed_data import seed as run_seed  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _seed_once():
    run_seed(reset=True)
    yield
    import shutil

    shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)


@pytest.fixture()
def seeded_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def demo_credentials():
    return {"email": DEMO_MERCHANT_EMAIL, "password": DEMO_MERCHANT_PASSWORD}


@pytest.fixture()
def seeded_client():
    """A TestClient that does NOT override get_db -- requests hit the real,
    shared on-disk seeded SQLite DB (same one `seeded_db` reads from) via
    the app's normal SessionLocal. Intended for read-only regression checks
    (e.g. logging in as a seeded account and hitting GET endpoints) --
    mutating requests here would leak state across tests/files since the
    seed only runs once per session.
    """
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Isolated in-memory DB for tests that want a clean slate.
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
