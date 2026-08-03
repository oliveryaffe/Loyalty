"""Application settings, sourced from environment variables (with sane
local-dev defaults so `uvicorn app.main:app` works out of the box).

No secrets are committed: JWT_SECRET_KEY has a dev default but should be
overridden via the JWT_SECRET_KEY env var (or a `.env` file, see .env.example)
in any shared/deployed environment.
"""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database: SQLite by default for MVP/local dev. Point DATABASE_URL at a
    # Postgres DSN (e.g. postgresql+psycopg2://user:pass@host/db) for a real
    # deployment -- SQLAlchemy models are portable, no SQLite-only features used.
    database_url: str = "sqlite:///./loyalty.db"

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        """Rewrite Railway/Heroku-style `postgres://` (and bare
        `postgresql://`) DSNs to `postgresql+psycopg2://` -- SQLAlchemy 2.x
        rejects the bare `postgres://` scheme, and Railway's Postgres plugin
        injects DATABASE_URL in that exact form. SQLite URLs and DSNs that
        already specify a driver (`postgresql+psycopg2://`, etc.) pass
        through unchanged.
        """
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+psycopg2://", 1)
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+psycopg2://", 1)
        return v

    jwt_secret_key: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 12  # 12 hours

    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # Points program defaults
    points_per_pound: float = 1.0

    # Sanity ceiling on a single ingested transaction's pound amount, to
    # guard against a fat-fingered or malicious ingestion call minting an
    # unbounded number of points in one request. £50,000 comfortably covers
    # any plausible single-purchase loyalty event for this MVP's retail use
    # case; raise via env var if a merchant's use case genuinely needs more.
    max_transaction_amount_gbp: float = 50_000.0

    # Fraud detector tuning
    fraud_zscore_threshold: float = 3.0
    fraud_velocity_window_hours: int = 24
    fraud_velocity_max_txns: int = 5


settings = Settings()
