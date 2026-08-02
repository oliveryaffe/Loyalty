"""Core points-ledger business logic: earn and redeem rules.

This is the non-AI "foundational" loyalty engine (PLAN.md §3). Kept as pure
functions taking a SQLAlchemy Session + ORM objects so it's easy to unit
test without spinning up the HTTP layer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import floor

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    Member,
    MemberTier,
    Redemption,
    RedemptionStatus,
    RewardCatalogItem,
    Transaction,
    TransactionType,
)

TIER_RANK = {
    MemberTier.BRONZE.value: 0,
    MemberTier.SILVER.value: 1,
    MemberTier.GOLD.value: 2,
    MemberTier.PLATINUM.value: 3,
}


class LedgerError(Exception):
    """Base class for ledger validation failures."""


class InsufficientBalanceError(LedgerError):
    pass


class TierIneligibleError(LedgerError):
    pass


class InactiveMemberError(LedgerError):
    pass


class RewardUnavailableError(LedgerError):
    pass


def points_for_purchase(amount_usd: float) -> int:
    """Earn rule: $1 spent = `points_per_dollar` points (default 1:1),
    floored to the nearest whole point."""
    if amount_usd < 0:
        raise ValueError("amount_usd must be non-negative")
    return floor(amount_usd * settings.points_per_dollar)


def earn_points(
    db: Session,
    member: Member,
    amount_usd: float,
    channel: str = "pos",
    occurred_at: datetime | None = None,
) -> Transaction:
    """Record a purchase and credit points to the member's balance.

    Returns the created Transaction. Caller is responsible for db.commit().
    """
    if not member.is_active:
        raise InactiveMemberError(f"Member {member.id} is not active")

    points = points_for_purchase(amount_usd)
    txn = Transaction(
        member_id=member.id,
        type=TransactionType.EARN.value,
        amount_usd=amount_usd,
        points=points,
        channel=channel,
    )
    if occurred_at is not None:
        txn.created_at = occurred_at

    member.points_balance += points
    member.last_activity_at = occurred_at or datetime.now(timezone.utc)

    db.add(txn)
    db.flush()
    return txn


def redeem_reward(
    db: Session,
    member: Member,
    reward: RewardCatalogItem,
) -> tuple[Redemption, Transaction | None]:
    """Attempt to redeem `reward` for `member`.

    Validates: member active, reward active, tier eligibility, sufficient
    points balance. On success, debits the balance and creates both a
    Redemption row and a matching negative-points Transaction (for ledger
    completeness). On failure, raises a LedgerError subclass *and* still
    records a rejected Redemption row for audit purposes -- callers that
    want the audit trail even on failure should catch the exception after
    calling `redeem_reward_safe` instead.

    Concurrency note: the balance check-and-debit is done as a single
    atomic `UPDATE ... WHERE points_balance >= cost` statement (see below)
    rather than a Python-level "if balance >= cost: balance -= cost" (which
    is a classic TOCTOU/lost-update race -- two concurrent callers can both
    read a sufficient balance before either has written its debit, letting
    both redeem against the same points). Only one concurrent caller can
    ever match the WHERE clause and flip the row; the rest see `rowcount
    == 0` and are correctly rejected, regardless of how many race in at
    once. This holds under both SQLite and Postgres.
    """
    if not member.is_active:
        raise InactiveMemberError(f"Member {member.id} is not active")

    if not reward.active:
        raise RewardUnavailableError(f"Reward {reward.id} is not active")

    if TIER_RANK.get(member.tier, 0) < TIER_RANK.get(reward.tier_required, 0):
        raise TierIneligibleError(
            f"Member tier '{member.tier}' does not meet required tier '{reward.tier_required}'"
        )

    result = db.execute(
        update(Member)
        .where(
            Member.id == member.id,
            Member.points_balance >= reward.points_cost,
        )
        .values(points_balance=Member.points_balance - reward.points_cost)
    )
    if result.rowcount == 0:
        # Either the balance was insufficient, or another concurrent
        # redemption already consumed it out from under us. Re-read the
        # current balance purely for the error message; the WHERE clause
        # above is what actually enforced correctness.
        db.refresh(member)
        raise InsufficientBalanceError(
            f"Member has {member.points_balance} points, needs {reward.points_cost}"
        )

    # Sync the in-memory ORM object with the value the atomic UPDATE just
    # wrote (the UPDATE above went through Core and bypasses the ORM's
    # identity-map attribute tracking).
    db.refresh(member)
    member.last_activity_at = datetime.now(timezone.utc)

    txn = Transaction(
        member_id=member.id,
        type=TransactionType.REDEEM.value,
        amount_usd=0.0,
        points=-reward.points_cost,
        channel="redemption",
    )
    db.add(txn)
    db.flush()

    redemption = Redemption(
        member_id=member.id,
        reward_id=reward.id,
        transaction_id=txn.id,
        points_spent=reward.points_cost,
        status=RedemptionStatus.COMPLETED.value,
    )
    db.add(redemption)
    db.flush()
    return redemption, txn


def redeem_reward_safe(db: Session, member: Member, reward: RewardCatalogItem) -> Redemption:
    """Same as redeem_reward, but on validation failure records a rejected
    Redemption row instead of raising, and returns it (status != completed).
    Raises only on programmer errors (missing member/reward), not business
    rule violations.
    """
    try:
        redemption, _txn = redeem_reward(db, member, reward)
        return redemption
    except InsufficientBalanceError:
        status = RedemptionStatus.REJECTED_INSUFFICIENT_BALANCE.value
    except TierIneligibleError:
        status = RedemptionStatus.REJECTED_TIER.value
    except InactiveMemberError:
        status = RedemptionStatus.REJECTED_INACTIVE.value
    except RewardUnavailableError:
        status = RedemptionStatus.REJECTED_INACTIVE.value

    redemption = Redemption(
        member_id=member.id,
        reward_id=reward.id,
        transaction_id=None,
        points_spent=0,
        status=status,
    )
    db.add(redemption)
    db.flush()
    return redemption
