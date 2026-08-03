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

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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

    # Stripe billing (PLAN_BATCH3.md §2). All nullable/additive -- safe under
    # app/db/base.py's _sync_missing_columns sweep, same convention as every
    # other column added post-launch. subscription_status mirrors Stripe's
    # own status strings (trialing/active/past_due/canceled/unpaid/
    # incomplete/incomplete_expired) verbatim rather than a local enum, so
    # webhook handlers can write it through unchanged -- see
    # app/api/deps.py::require_active_subscription for how these are
    # interpreted (trialing/active/past_due allowed through, everything else
    # -- including NULL, meaning "never subscribed" -- hard-locked).
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    subscription_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    subscription_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)  # starter/growth/scale
    subscription_current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Notifications (PLAN_BATCH3.md §3). All nullable/additive -- same
    # convention as the Stripe columns above. Slack webhook URL is
    # self-serve per merchant (each merchant pastes their own Slack
    # "Incoming Webhooks" URL); email requires the owner to have supplied
    # platform-level SMTP credentials (app/config.py) before it actually
    # sends anything. notify_on_churn_risk/notify_on_fraud_alert are
    # nullable booleans -- existing merchant rows read back as NULL after
    # the ALTER (see "Migration approach" in the plan), so application
    # code must never read these attributes directly: use
    # app/services/notifications.py::wants_churn_notifications /
    # wants_fraud_notifications, which treat NULL the same as True.
    notification_slack_webhook_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notification_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notify_on_churn_risk: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    notify_on_fraud_alert: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

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

    # GDPR erasure (UK GDPR right to erasure / Art. 17). Nullable/additive --
    # safe under app/db/base.py's _sync_missing_columns sweep. NULL means
    # "never erased" (the overwhelming majority of rows); set once, by
    # POST /api/v1/members/{id}/gdpr-erase, and never cleared again.
    # Deliberately NOT a hard delete -- see app/api/members.py::gdpr_erase_member
    # for the anonymize-in-place rationale (Transaction/Redemption/FraudAlert
    # rows for this member are preserved so merchant business records and AI
    # training data don't silently lose data points on every erasure request).
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Churn-escalation notification dedup (PLAN_BATCH3.md §3). Both
    # nullable/additive. `last_known_risk_band` mirrors the band computed
    # by app/ai/churn_model.py the last time a request-triggered recompute
    # observed this member (kept in sync for every band, not just "high"),
    # so a later escalation is detected as a genuine low/medium -> high
    # *transition*, not just "currently high" (which would re-fire on
    # every dashboard refresh). `risk_escalated_notified_at` is stamped
    # only on a winning transition and doubles as the cooldown clock (see
    # app/services/notifications.py::check_churn_escalations) -- an
    # oscillation safety net so a member stuck at "high" for a very long
    # time without ever dropping still gets re-notified at most once per
    # `settings.notification_cooldown_hours`, not zero times forever.
    last_known_risk_band: Mapped[str | None] = mapped_column(String(20), nullable=True)
    risk_escalated_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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

    # Win-back campaigns (PLAN_BATCH3.md §4). Nullable/additive, same
    # convention as every other post-launch column -- existing rows read
    # back as NULL, which app code never needs to special-case since
    # nothing reads this column for pre-existing redemptions. "manual"
    # (the default for new ORM-created rows going forward) covers ordinary
    # staff-processed redemptions via POST /rewards/redeem; "winback"
    # marks a comped redemption created by
    # app/services/winback.py::grant_winback_reward.
    source: Mapped[str | None] = mapped_column(String(20), default="manual", nullable=True)

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


class BillingEvent(Base):
    """Idempotency + audit log for Stripe webhook deliveries (PLAN_BATCH3.md
    §2). Exactly the same lesson this codebase already learned twice
    (Transaction.external_order_id's unique constraint for Shopify webhook
    dedup, app/services/shopify.py) applied to Stripe: webhooks can be
    redelivered, so a DB-level UNIQUE constraint on the event id -- not a
    SELECT-then-branch check -- is the actual source of truth. See
    app/api/billing.py::stripe_webhook, which inserts this row first and
    treats the resulting IntegrityError as "already processed, no-op".

    Brand-new table, so Base.metadata.create_all handles it directly --
    zero migration risk (see "Migration approach" in PLAN_BATCH3.md)."""

    __tablename__ = "billing_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    stripe_event_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    merchant_id: Mapped[str | None] = mapped_column(ForeignKey("merchants.id"), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    raw_payload: Mapped[str] = mapped_column(Text, default="")


class WinbackRule(Base):
    """One reward preference per merchant, used by the win-back worklist
    (app/services/winback.py::get_winback_worklist) to suggest what to
    offer an at-risk member. `unique=True` on merchant_id enforces "one
    preference per merchant", not a multi-rule campaign builder.
    `churn_risk_threshold` defaults to app.ai.churn_model.RISK_BAND_MEDIUM_MAX
    (65.0), i.e. "high" band by default.

    REWORKED: this table originally drove an auto-executing campaign --
    `enabled`/`auto_trigger` gated whether the app granted a free reward on
    the merchant's behalf. That assumed Ledgerly was the merchant's system
    of record for redemptions, which doesn't fit the insights-layer
    positioning (a "completed" redemption the merchant's real POS and the
    customer never saw was misleading). The worklist is now purely
    read-only and doesn't grant anything, so `enabled` is now just "have I
    configured a reward suggestion" and `auto_trigger` is unused/ignored by
    the app -- left in place rather than dropped since this repo has no
    migration tooling for column removal (see app/db/base.py::init_db).

    Brand-new table -- create_all handles it directly, zero migration risk."""

    __tablename__ = "winback_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    churn_risk_threshold: Mapped[float] = mapped_column(Float, default=65.0, nullable=False)
    reward_id: Mapped[str] = mapped_column(ForeignKey("reward_catalog_items.id"), nullable=False)
    auto_trigger: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # unused, see docstring
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class WinbackOffer(Base):
    """UNUSED as of the win-back rework -- nothing in the app writes to
    this table anymore (see app/services/winback.py). Kept defined, not
    dropped, because this repo has no migration tooling for table removal
    (app/db/base.py::init_db only adds tables/columns, never drops them)
    and dropping the class would orphan any rows already written by
    earlier versions of this feature in production. New code should not
    read or write this table -- the win-back worklist is computed live and
    persists nothing."""

    __tablename__ = "winback_offers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False, index=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id"), nullable=False, unique=True)
    rule_id: Mapped[str] = mapped_column(ForeignKey("winback_rules.id"), nullable=False)
    redemption_id: Mapped[str] = mapped_column(ForeignKey("redemptions.id"), nullable=False)
    churn_risk_score_at_trigger: Mapped[float] = mapped_column(Float, nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False)  # "manual" | "auto"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RewardExperiment(Base):
    """A/B test of two reward variants (PLAN_BATCH3.md §5) -- a merchant-admin
    dashboard tool, not a consumer-facing page split: "assignment" here is a
    backend cohort split (which arm a member is in) that (1) steers
    app/ai/recommender.py::recommend_for_member toward each member's
    assigned variant and (2) is measured by comparing redemption behavior
    between the two cohorts. Deliberately the smallest-scope MVP shape --
    random assignment + a results comparison view, not a full
    experimentation platform.

    Brand-new table -- create_all handles it directly, zero migration risk."""

    __tablename__ = "reward_experiments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    variant_a_reward_id: Mapped[str] = mapped_column(ForeignKey("reward_catalog_items.id"), nullable=False)
    variant_b_reward_id: Mapped[str] = mapped_column(ForeignKey("reward_catalog_items.id"), nullable=False)
    traffic_split: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)  # fraction assigned to B
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)  # running | completed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExperimentAssignment(Base):
    """Which arm ("a"/"b") a member was randomly assigned to for a given
    experiment (PLAN_BATCH3.md §5). Bulk-assigned once, at experiment
    creation time, over every active member of the merchant -- deterministic
    (SHA-256 hash of `f"{experiment_id}:{member_id}"`, see
    app/services/experiments.py::assign_variant), so re-fetching a member's
    assignment never flips it, without needing to persist any RNG seed.
    `UniqueConstraint(experiment_id, member_id)` is defense-in-depth (the
    primary guard is simply that assignment only ever happens once, in the
    single bulk pass at creation -- no re-assignment endpoint exists).

    Brand-new table -- create_all handles it directly, zero migration risk."""

    __tablename__ = "experiment_assignments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("reward_experiments.id"), nullable=False, index=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id"), nullable=False, index=True)
    variant: Mapped[str] = mapped_column(String(1), nullable=False)  # "a" | "b"
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("experiment_id", "member_id"),)
