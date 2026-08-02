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

engine = create_engine(settings.database_url, connect_args=connect_args)
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
    """Create all tables. Fine for MVP/SQLite; a real deployment would use
    Alembic migrations instead."""
    from app.db import models  # noqa: F401  (ensure models are registered)

    Base.metadata.create_all(bind=engine)
