"""Personalized reward recommendation unit tests (app/ai/recommender.py)."""
from collections import Counter

import pytest

from app.ai.recommender import recommend_for_member, score_rewards_for_member
from app.db.models import ExperimentAssignment, Member, Merchant, Redemption, RewardCatalogItem, RewardExperiment


@pytest.fixture()
def merchant(db_session):
    m = Merchant(business_name="Test Co")
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


def test_recommend_for_member_falls_back_when_experiment_exclusion_would_zero_out_recommendations(
    db_session, merchant
):
    """TEST_REPORT_BATCH3.md §6 (MEDIUM): live-reproduced with a small
    2-reward catalog -- a member's own assigned A/B variant reward is
    tier-inaccessible to them, and the *other* variant's reward (their only
    other option) would normally be excluded by experiment steering
    (`_excluded_reward_ids_for_member`), leaving them with zero
    recommendations they'd otherwise have had. Experiment steering is
    best-effort and must fall back to NOT excluding the other variant's
    reward when doing so would produce an empty list."""
    member = Member(
        merchant_id=merchant.id,
        first_name="Grace",
        last_name="Hopper",
        email="grace-fallback@example.com",
        tier="bronze",
        points_balance=1000,
    )
    db_session.add(member)

    # Exactly two rewards in the whole catalog, mirroring the report's
    # "A-gold-only" / "B-any-tier" repro.
    gold_only = RewardCatalogItem(
        merchant_id=merchant.id,
        name="A-gold-only",
        points_cost=100,
        tier_required="gold",
        active=True,
        category="general",
    )
    any_tier = RewardCatalogItem(
        merchant_id=merchant.id,
        name="B-any-tier",
        points_cost=100,
        tier_required="bronze",
        active=True,
        category="general",
    )
    db_session.add_all([gold_only, any_tier])
    db_session.flush()

    experiment = RewardExperiment(
        merchant_id=merchant.id,
        name="Small catalog experiment",
        variant_a_reward_id=gold_only.id,
        variant_b_reward_id=any_tier.id,
        traffic_split=0.5,
        status="running",
    )
    db_session.add(experiment)
    db_session.flush()

    # Member is assigned to variant "a" (gold_only) -- tier-inaccessible to
    # this bronze member on its own merits. Steering would normally also
    # exclude variant "b"'s reward (any_tier) as "the other variant", which
    # -- before the fallback -- left this member with an empty list despite
    # any_tier being a perfectly valid option absent the experiment.
    db_session.add(ExperimentAssignment(experiment_id=experiment.id, member_id=member.id, variant="a"))
    db_session.commit()

    ranked = recommend_for_member(db_session, member, top_n=5)

    assert len(ranked) == 1
    assert ranked[0].reward.id == any_tier.id
