"""Personalized reward recommendation unit tests (app/ai/recommender.py)."""
from collections import Counter

import pytest

from app.ai.recommender import recommend_for_member, score_rewards_for_member
from app.db.models import Member, Merchant, Redemption, RewardCatalogItem


@pytest.fixture()
def merchant(db_session):
    m = Merchant(business_name="Test Co", email="owner@test.co", hashed_password="x")
    db_session.add(m)
    db_session.flush()
    return m


def test_affordable_reward_outranks_unaffordable_reward(db_session, merchant):
    member = Member(
        merchant_id=merchant.id,
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        tier="bronze",
        points_balance=100,
    )
    cheap = RewardCatalogItem(
        merchant_id=merchant.id, name="Cheap", points_cost=100, tier_required="bronze", active=True, category="general"
    )
    expensive = RewardCatalogItem(
        merchant_id=merchant.id, name="Expensive", points_cost=5000, tier_required="bronze", active=True, category="general"
    )
    ranked = score_rewards_for_member(member, [cheap, expensive], Counter(), Counter())

    assert ranked[0].reward.name == "Cheap"
    assert ranked[0].score > ranked[1].score


def test_tier_ineligible_rewards_are_excluded(db_session, merchant):
    member = Member(
        merchant_id=merchant.id,
        first_name="Ada",
        last_name="Lovelace",
        email="ada2@example.com",
        tier="bronze",
        points_balance=100_000,
    )
    gold_reward = RewardCatalogItem(
        merchant_id=merchant.id, name="Gold Only", points_cost=100, tier_required="gold", active=True
    )
    ranked = score_rewards_for_member(member, [gold_reward], Counter(), Counter())
    assert ranked == []


def test_inactive_rewards_are_excluded(db_session, merchant):
    member = Member(
        merchant_id=merchant.id, first_name="A", last_name="B", email="c@d.com", tier="bronze", points_balance=1000
    )
    inactive_reward = RewardCatalogItem(
        merchant_id=merchant.id, name="Retired", points_cost=100, tier_required="bronze", active=False
    )
    ranked = score_rewards_for_member(member, [inactive_reward], Counter(), Counter())
    assert ranked == []


def test_category_affinity_boosts_matching_category(db_session, merchant):
    member = Member(
        merchant_id=merchant.id, first_name="A", last_name="B", email="e@f.com", tier="bronze", points_balance=10000
    )
    coffee = RewardCatalogItem(
        merchant_id=merchant.id, name="Coffee", points_cost=500, tier_required="bronze", active=True, category="beverage"
    )
    gadget = RewardCatalogItem(
        merchant_id=merchant.id, name="Gadget", points_cost=500, tier_required="bronze", active=True, category="electronics"
    )
    # Member has a strong history of redeeming "beverage" category rewards.
    affinity = Counter({"beverage": 5})
    ranked = score_rewards_for_member(member, [coffee, gadget], affinity, Counter())

    assert ranked[0].reward.name == "Coffee"
    assert ranked[0].score > ranked[1].score


def test_recommend_for_member_end_to_end_via_db(db_session, merchant):
    member = Member(
        merchant_id=merchant.id, first_name="Ada", last_name="L", email="ada3@example.com",
        tier="gold", points_balance=6000,
    )
    db_session.add(member)
    r1 = RewardCatalogItem(
        merchant_id=merchant.id, name="Affordable Gadget", points_cost=4000, tier_required="bronze",
        active=True, category="electronics",
    )
    r2 = RewardCatalogItem(
        merchant_id=merchant.id, name="Out of Reach", points_cost=1_000_000, tier_required="bronze",
        active=True, category="electronics",
    )
    db_session.add_all([r1, r2])
    db_session.flush()

    results = recommend_for_member(db_session, member, top_n=5)
    assert len(results) == 2
    assert results[0].reward.name == "Affordable Gadget"
    # Non-empty, plausible ranked list -- acceptance criterion.
    assert all(r.score >= 0 for r in results)
