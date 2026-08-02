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


class Merchant(Base):
    """A B2B customer (retailer) account. Merchant admins log in with these
    credentials to reach the dashboard."""

    __tablename__ = "merchants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    members: Mapped[list["Member"]] = relationship(back_populates="merchant")
    rewards: Mapped[list["RewardCatalogItem"]] = relationship(back_populates="merchant")


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
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)  # purchase $ amount (earn only)
    points: Mapped[int] = mapped_column(Integer, nullable=False)  # signed: + earn, - redeem
    channel: Mapped[str] = mapped_column(String(30), default="pos")  # pos, online, mobile
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)

    # Synthetic-data-only marker: was this transaction deliberately injected
    # as a fraud-like pattern by the seed script? Used only by tests to
    # measure detector recall/precision -- never fed to the detector itself.
    synthetic_fraud_label: Mapped[bool] = mapped_column(Boolean, default=False)

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
