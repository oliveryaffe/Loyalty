"""SQLAlchemy engine/session/declarative-base setup."""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# `timeout`: on SQLite, concurrent writers (e.g. two simultaneous redemption
# requests hitting the atomic UPDATE in app/services/ledger.py) block on
# SQLite's file-level write lock. The pysqlite default is 5s; bump it so a
# short-lived burst of concurrent requests waits and serializes cleanly
# instead of surfacing as a raw `sqlite3.OperationalError: database is
# locked` 500 to the client. Not applicable to Postgres.
connect_args = (
    {"check_same_thread": False, "timeout": 30} if settings.database_url.startswith("sqlite") else {}
)

# pool_pre_ping: harmless for SQLite, important for Postgres -- Railway's
# managed Postgres can silently drop idle connections, and without
# pre-ping the next request would get a raw OperationalError instead of a
# clean reconnect.
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all (missing) tables -- non-destructive, only adds tables that
    don't already exist. Fine for MVP/SQLite and for the clean-cutover
    assumption Postgres is brought up under (Batch 1); a real deployment
    with evolving schema requirements would eventually move to Alembic
    migrations instead (explicitly deferred, not forgotten -- see
    PLAN_BATCH1.md Feature 1)."""
    from app.db import models  # noqa: F401  (ensure models are registered)

    Base.metadata.create_all(bind=engine)
