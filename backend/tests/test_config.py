"""DATABASE_URL normalization (Feature 1): Railway/Heroku-style
`postgres://` and bare `postgresql://` DSNs must both be rewritten to
`postgresql+psycopg2://` at Settings-construction time -- SQLAlchemy 2.x
rejects the bare `postgres://` scheme. SQLite and DSNs that already name a
driver must pass through unchanged. This is a pure unit test of the
validator -- no live Postgres required.
"""
from app.config import Settings


def test_database_url_normalization_postgres_scheme():
    s = Settings(database_url="postgres://u:p@h/d")
    assert s.database_url == "postgresql+psycopg2://u:p@h/d"


def test_database_url_normalization_postgresql_scheme():
    s = Settings(database_url="postgresql://u:p@h/d")
    assert s.database_url == "postgresql+psycopg2://u:p@h/d"


def test_database_url_normalization_already_has_driver_is_unchanged():
    s = Settings(database_url="postgresql+psycopg2://u:p@h/d")
    assert s.database_url == "postgresql+psycopg2://u:p@h/d"


def test_database_url_normalization_sqlite_passthrough():
    s = Settings(database_url="sqlite:///./loyalty.db")
    assert s.database_url == "sqlite:///./loyalty.db"


def test_database_url_normalization_sqlite_memory_passthrough():
    s = Settings(database_url="sqlite://")
    assert s.database_url == "sqlite://"
