"""Ledger math unit tests: earn/redeem business rules in app/services/ledger.py."""
import pytest

from app.db.models import Member, Merchant, RewardCatalogItem
from app.services.ledger import (
    InactiveMemberError,
    InsufficientBalanceError,
    RewardUnavailableError,
    TierIneligibleError,
    earn_points,
    points_for_purchase,
    redeem_reward,
    redeem_reward_safe,
)


@pytest.fixture()
def merchant(db_session):
    m = Merchant(business_name="Test Co", email="owner@test.co", hashed_password="x")
    db_session.add(m)
    db_session.flush()
    return m


@pytest.fixture()
def member(db_session, merchant):
    m = Member(
        merchant_id=merchant.id,
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        tier="bronze",
        points_balance=0,
    )
    db_session.add(m)
    db_session.flush()
    return m


def test_points_for_purchase_is_floor_of_amount_times_rate():
    assert points_for_purchase(10.0) == 10
    assert points_for_purchase(10.99) == 10  # floored, not rounded
    assert points_for_purchase(0) == 0


def test_earn_points_increases_balance(db_session, member):
    assert member.points_balance == 0
    txn = earn_points(db_session, member, amount_usd=42.50, channel="pos")
    db_session.commit()

    assert txn.points == 42
    assert txn.type == "earn"
    assert member.points_balance == 42


def test_earn_points_accumulates_across_multiple_transactions(db_session, member):
    earn_points(db_session, member, amount_usd=10.0)
    earn_points(db_session, member, amount_usd=25.0)
    earn_points(db_session, member, amount_usd=3.30)
    db_session.commit()

    assert member.points_balance == 10 + 25 + 3


def test_earn_points_rejects_inactive_member(db_session, member):
    member.is_active = False
    db_session.flush()
    with pytest.raises(InactiveMemberError):
        earn_points(db_session, member, amount_usd=10.0)


@pytest.fixture()
def reward(db_session, merchant):
    r = RewardCatalogItem(
        merchant_id=merchant.id,
        name="Free Coffee",
        points_cost=100,
        tier_required="bronze",
        active=True,
    )
    db_session.add(r)
    db_session.flush()
    return r


def test_redeem_reward_success_debits_balance(db_session, member, reward):
    earn_points(db_session, member, amount_usd=150.0)
    db_session.commit()
    assert member.points_balance == 150

    redemption, txn = redeem_reward(db_session, member, reward)
    db_session.commit()

    assert redemption.status == "completed"
    assert redemption.points_spent == 100
    assert member.points_balance == 50
    assert txn.points == -100
    assert txn.type == "redeem"


def test_redeem_reward_insufficient_balance_raises_and_does_not_debit(db_session, member, reward):
    earn_points(db_session, member, amount_usd=10.0)  # only 10 points, need 100
    db_session.commit()

    with pytest.raises(InsufficientBalanceError):
        redeem_reward(db_session, member, reward)

    assert member.points_balance == 10  # unchanged


def test_redeem_reward_tier_ineligible(db_session, member, reward):
    reward.tier_required = "gold"
    member.tier = "bronze"
    member.points_balance = 10_000
    db_session.flush()

    with pytest.raises(TierIneligibleError):
        redeem_reward(db_session, member, reward)


def test_redeem_reward_inactive_reward(db_session, member, reward):
    reward.active = False
    member.points_balance = 10_000
    db_session.flush()

    with pytest.raises(RewardUnavailableError):
        redeem_reward(db_session, member, reward)


def test_redeem_reward_safe_records_rejected_redemption_without_raising(db_session, member, reward):
    member.points_balance = 0
    db_session.flush()

    redemption = redeem_reward_safe(db_session, member, reward)
    db_session.commit()

    assert redemption.status == "rejected_insufficient_balance"
    assert redemption.points_spent == 0
    assert member.points_balance == 0
