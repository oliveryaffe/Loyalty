"""SQLAlchemy ORM models for the loyalty ledger.

Tables: Merchant (B2B admin account), Member (end shopper), Transaction
(points ledger entries), RewardCatalogItem, Redemption, FraudAlert.

Kept intentionally single-tenant-per-merchant-row simple: Member/Reward rows
carry a merchant_id so the schema *could* support multiple merchants, but the
MVP seed script and dashboard only exercise a single merchant.
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex


class TransactionType(str, enum.Enum):
    EARN = "earn"
    REDEEM = "redeem"
    ADJUST = "adjust"


class RedemptionStatus(str, enum.Enum):
    COMPLETED = "completed"
    REJECTED_INSUFFICIENT_BALANCE = "rejected_insufficient_balance"
    REJECTED_TIER = "rejected_tier"
    REJECTED_INACTIVE = "rejected_inactive"


class MemberTier(str, enum.Enum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    PLATINUM = "platinum"


class TeamRole(str, enum.Enum):
    ADMIN = "admin"
    MEMBER = "member"


class Merchant(Base):
    """A B2B customer (retailer) account -- a pure business entity. Login
    credentials live on TeamMember (see below), not here: a Merchant can
    have multiple human users (team members) with different roles."""

    __tablename__ = "merchants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # Shopify webhook ingestion config (Feature 2). Per-merchant secret,
    # not a single global secret -- different merchants would have
    # different Shopify apps/secrets in production.
    shopify_webhook_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shopify_shop_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)

    members: Mapped[list["Member"]] = relationship(back_populates="merchant")
    rewards: Mapped[list["RewardCatalogItem"]] = relationship(back_populates="merchant")
    team_members: Mapped[list["TeamMember"]] = relationship(
        back_populates="merchant", cascade="all, delete-orphan"
    )


class TeamMember(Base):
    """A human user who can log in to a Merchant's dashboard. Not to be
    confused with `Member`, which means "end loyalty-program shopper" in
    this codebase. Multiple TeamMembers can belong to one Merchant, with
    different roles (admin/member)."""

    __tablename__ = "team_members"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False, index=True)

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default=TeamRole.MEMBER.value, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    merchant: Mapped["Merchant"] = relationship(back_populates="team_members")


class Member(Base):
    """An end shopper enrolled in a merchant's loyalty program."""

    __tablename__ = "members"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False, index=True)

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    points_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tier: Mapped[str] = mapped_column(String(20), default=MemberTier.BRONZE.value, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # Synthetic-data-only marker so tests/seed can assert cohort membership
    # without re-deriving it from behavior. Not exposed as ML input.
    synthetic_cohort: Mapped[str | None] = mapped_column(String(30), nullable=True)

    merchant: Mapped["Merchant"] = relationship(back_populates="members")
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="member", cascade="all, delete-orphan"
    )
    redemptions: Mapped[list["Redemption"]] = relationship(
        back_populates="member", cascade="all, delete-orphan"
    )


class RewardCatalogItem(Base):
    __tablename__ = "reward_catalog_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(50), default="general")
    points_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    tier_required: Mapped[str] = mapped_column(String(20), default=MemberTier.BRONZE.value)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    merchant: Mapped["Merchant"] = relationship(back_populates="rewards")
    redemptions: Mapped[list["Redemption"]] = relationship(back_populates="reward")


class Transaction(Base):
    """A single points ledger entry: an earn event (from a purchase) or a
    redeem event (from a reward redemption) or a manual adjustment."""

    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id"), nullable=False, index=True)

    type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_gbp: Mapped[float] = mapped_column(Float, default=0.0)  # purchase £ amount (earn only)
    points: Mapped[int] = mapped_column(Integer, nullable=False)  # signed: + earn, - redeem
    channel: Mapped[str] = mapped_column(String(30), default="pos")  # pos, online, mobile
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)

    # Shopify webhook ingestion (Feature 2). external_order_id is Shopify's
    # own order `id`, used for idempotency/dedup on webhook redelivery.
    # source distinguishes manual POST /transactions calls from
    # webhook-ingested ones. Both nullable/defaulted so no existing row is
    # invalidated by this addition.
    #
    # `unique=True`: a plain SELECT-then-INSERT idempotency check (as
    # app/services/shopify.py used to rely on exclusively) is a TOCTOU race
    # -- two concurrent deliveries of the same webhook can both pass the
    # "does a Transaction with this external_order_id already exist?" check
    # before either commits. The DB-level UNIQUE constraint is the actual
    # source of truth for dedup; app/services/shopify.py catches the
    # resulting IntegrityError and treats it as "already processed". NULL
    # values (regular non-webhook transactions) are not considered
    # duplicates of each other by any standard SQL UNIQUE constraint
    # (Postgres and SQLite both treat NULL <> NULL for uniqueness), so this
    # is safe for the many Transaction rows that have no external_order_id.
    external_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    source: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)

    # Synthetic-data-only marker: was this transaction deliberately injected
    # as a fraud-like pattern by the seed script? Used only by tests to
    # measure detector recall/precision -- never fed to the detector itself.
    synthetic_fraud_label: Mapped[bool] = mapped_column(Boolean, default=False)

    # Product-level detail (Batch 2, PLAN_BATCH2.md §1). Nullable/additive --
    # same clean-cutover reasoning as external_order_id/source above, no
    # migration needed. Populated by the CSV upload path
    # (app/services/csv_ingest.py); ordinary manual/Shopify-ingested
    # transactions leave both null. Kept directly on Transaction rather than
    # a separate product table -- see PLAN_BATCH2.md §1 for the full
    # rationale (one purchase event = one row, no multi-line-item orders in
    # scope, avoids a second ledger to reconcile).
    product_category: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    product_name: Mapped[str | None] = mapped_column(String(150), nullable=True)

    member: Mapped["Member"] = relationship(back_populates="transactions")
    fraud_alerts: Mapped[list["FraudAlert"]] = relationship(back_populates="transaction")


class Redemption(Base):
    __tablename__ = "redemptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id"), nullable=False, index=True)
    reward_id: Mapped[str] = mapped_column(ForeignKey("reward_catalog_items.id"), nullable=False)
    transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)

    points_spent: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    member: Mapped["Member"] = relationship(back_populates="redemptions")
    reward: Mapped["RewardCatalogItem"] = relationship(back_populates="redemptions")


class FraudAlert(Base):
    __tablename__ = "fraud_alerts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), nullable=False, index=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id"), nullable=False, index=True)

    reason: Mapped[str] = mapped_column(String(120), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)  # higher = more suspicious
    details: Mapped[str] = mapped_column(Text, default="")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    transaction: Mapped["Transaction"] = relationship(back_populates="fraud_alerts")
    member: Mapped["Member"] = relationship()
