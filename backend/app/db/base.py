"""SQLAlchemy engine/session/declarative-base setup."""
import logging
from collections.abc import Generator
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.schema import CreateColumn

from app.config import settings

logger = logging.getLogger(__name__)

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


# Deliberate column renames across releases go here as (table, old, new).
# Applied via ALTER TABLE ... RENAME COLUMN -- unlike the generic
# add-missing-columns sweep, a straight rename preserves existing data
# and the original NOT NULL constraint in one atomic statement, and
# critically avoids `_sync_missing_columns` instead treating the new
# name as a brand-new column and trying to ADD COLUMN ... NOT NULL on a
# table that already has rows (which Postgres rejects without a server
# default -- would have taken the whole app down on next deploy).
# Currency rebrand (UK localisation): amount_usd -> amount_gbp is a pure
# relabel of the same numbers, not a unit conversion, so a rename (not a
# backfill) is correct here.
_COLUMN_RENAMES: list[tuple[str, str, str]] = [
    ("transactions", "amount_usd", "amount_gbp"),
]


def init_db() -> None:
    """Create all (missing) tables -- non-destructive, only adds tables that
    don't already exist. Fine for MVP/SQLite and for the clean-cutover
    assumption Postgres is brought up under (Batch 1); a real deployment
    with evolving schema requirements would eventually move to Alembic
    migrations instead (explicitly deferred, not forgotten -- see
    PLAN_BATCH1.md Feature 1).

    `create_all` alone only handles brand-new tables -- it silently does
    NOT add new columns to a table that already exists on disk. That gap
    bit us for real: Batch 2 added `product_category`/`product_name` to
    `transactions`, but the already-provisioned production Postgres
    volume kept the old schema, so every `/members` request 500'd with
    `UndefinedColumn` and the dashboard went blank right after login.
    `_sync_missing_columns` below is a minimal, idempotent stopgap --
    for each mapped table that already exists, diff its live columns
    against the ORM model and ALTER TABLE ADD COLUMN any that are
    missing. Still not a substitute for real migrations if columns are
    ever renamed/dropped/retyped -- for renames specifically, see
    `_apply_column_renames`, which must run first so a rename isn't
    mistaken for an addition."""
    from app.db import models  # noqa: F401  (ensure models are registered)

    Base.metadata.create_all(bind=engine)
    _apply_column_renames()
    _sync_missing_columns()
    _backfill_legacy_merchant_subscriptions()
    _backfill_legacy_reward_catalog_currency()


def _apply_column_renames() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    to_rename = []  # list[tuple[str, str, str]]
    for table_name, old_name, new_name in _COLUMN_RENAMES:
        if table_name not in existing_tables:
            continue
        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        if old_name in existing_columns and new_name not in existing_columns:
            to_rename.append((table_name, old_name, new_name))

    if not to_rename:
        return

    with engine.begin() as conn:
        for table_name, old_name, new_name in to_rename:
            logger.warning(
                "Schema rename: %s.%s -> %s.%s",
                table_name,
                old_name,
                table_name,
                new_name,
            )
            conn.exec_driver_sql(
                f"ALTER TABLE {table_name} RENAME COLUMN {old_name} TO {new_name}"
            )


def _sync_missing_columns() -> None:
    # Compute the full diff (existing tables/columns vs. the ORM model)
    # using a read-only Inspector pass *before* opening any write
    # transaction. Interleaving inspector.get_columns() reads with an
    # open engine.begin() write transaction on the same engine was
    # observed to make SQLite misreport already-added columns as
    # missing on a second pass (duplicate-column errors) -- doing all
    # reads first, then all writes, avoids that entirely.
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    to_add = []  # list[tuple[str, Column]]
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all just made it fresh -- already in sync
        existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing_columns:
                to_add.append((table.name, column))

    if not to_add:
        return

    with engine.begin() as conn:
        for table_name, column in to_add:
            ddl = f"ALTER TABLE {table_name} ADD COLUMN {CreateColumn(column).compile(engine)}"
            logger.warning(
                "Schema drift detected: adding missing column %s.%s",
                table_name,
                column.name,
            )
            conn.exec_driver_sql(ddl)


def _backfill_legacy_merchant_subscriptions() -> None:
    """One-time, idempotent data backfill -- not a schema change, but the
    same class of "existing production row doesn't match what new code
    assumes" problem `_sync_missing_columns` exists for, just at the row
    level instead of the column level.

    Batch 3's billing gating (`require_active_subscription` in
    app/api/deps.py) treats any `subscription_status` not in
    {trialing, active, past_due} as hard-locked. `_sync_missing_columns`
    adds the new `subscription_status` column as NULL for every row that
    already existed before this migration -- which would have instantly
    hard-locked the seeded demo merchant (and any other pre-Batch-3
    merchant) the moment this deployed, since NULL isn't in that allowed
    set. Every code path that creates a NEW merchant post-Batch-3 (signup
    in app/api/auth.py, both seed_data.py paths) already sets an explicit
    subscription_status -- so this only ever touches genuinely legacy
    rows, and is a no-op (0 rows matched) on any database that doesn't
    have one, including every fresh install and the test suite's SQLite
    fixtures."""
    inspector = inspect(engine)
    if "merchants" not in inspector.get_table_names():
        return
    if "subscription_status" not in {c["name"] for c in inspector.get_columns("merchants")}:
        return  # column itself doesn't exist yet on some other DB flavor/timing -- next run will catch it

    far_future = datetime.now(timezone.utc) + timedelta(days=3650)
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "UPDATE merchants SET subscription_status = 'active', "
                "subscription_tier = COALESCE(subscription_tier, 'growth'), "
                "subscription_current_period_end = COALESCE(subscription_current_period_end, :far_future) "
                "WHERE subscription_status IS NULL"
            ),
            {"far_future": far_future},
        )
        if result.rowcount:
            logger.warning(
                "Legacy merchant backfill: granted 'active' subscription_status to "
                "%d pre-existing merchant row(s) with no billing state, so Batch 3's "
                "subscription gating doesn't lock out accounts that predate billing.",
                result.rowcount,
            )


def _backfill_legacy_reward_catalog_currency() -> None:
    """One-time, idempotent data backfill for the same reason as
    `_backfill_legacy_merchant_subscriptions`, just for reward catalog
    text content instead of billing state.

    The UK-localisation batch relabelled `REWARD_CATALOG` in
    scripts/seed_data.py from `$5/$10/.../$100 Store Credit` to
    `£5/£10/.../£100 Store Credit` -- but that only changes what a
    *fresh* seed run produces. `seed_if_empty()` is a deliberate no-op
    against a database that already has data (see its docstring), so
    the `RewardCatalogItem` rows created by the original seed run kept
    their old dollar-sign names/descriptions indefinitely -- confirmed
    live on the deployed Rewards page after the currency rename had
    otherwise fully shipped. `REPLACE(column, '$', '£')` is a plain SQL
    built-in supported identically by both SQLite and Postgres, so this
    needs no per-row Python loop. Scoped to rows that still contain a
    literal '$' so it's a no-op after the first successful run, and a
    no-op on any database seeded fresh post-rename (which never had a
    '$' in these columns to begin with)."""
    inspector = inspect(engine)
    if "reward_catalog_items" not in inspector.get_table_names():
        return

    with engine.begin() as conn:
        result = conn.execute(
            text(
                "UPDATE reward_catalog_items SET "
                "name = REPLACE(name, '$', '£'), "
                "description = REPLACE(description, '$', '£') "
                "WHERE name LIKE '%$%' OR description LIKE '%$%'"
            )
        )
        if result.rowcount:
            logger.warning(
                "Legacy reward-catalog backfill: relabelled %d reward(s) from "
                "'$' to '£' in name/description (pre-dated the GBP currency rename).",
                result.rowcount,
            )
