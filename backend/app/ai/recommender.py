"""Personalized reward recommendation (PLAN.md §3.1).

Content-based / rule-boosted scoring -- not a trained model at MVP scale,
but structured as a pure scoring function (`score_rewards_for_member`) so a
real collaborative-filtering or learned-ranking model could be swapped in
later behind the same interface.

Signals blended:
- **Affordability**: can the member redeem this now (partial credit if close)?
- **Category affinity**: does this match categories the member has redeemed
  before?
- **Popularity**: overall redemption rate across all members (cold-start
  fallback for members with no redemption history yet).
- Tier-ineligible or inactive rewards are excluded entirely (they are not a
  valid "next best action").
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db.models import ExperimentAssignment, Member, Redemption, RewardCatalogItem, RewardExperiment
from app.services.ledger import TIER_RANK

WEIGHT_AFFORDABILITY = 0.45
WEIGHT_AFFINITY = 0.35
WEIGHT_POPULARITY = 0.20


@dataclass
class RewardScore:
    reward: RewardCatalogItem
    score: float
    reason: str


def _affordability_score(member: Member, reward: RewardCatalogItem) -> float:
    if reward.points_cost <= 0:
        return 1.0
    ratio = member.points_balance / reward.points_cost
    return max(0.0, min(1.0, ratio))


def _category_affinity(category_counts: Counter, category: str) -> float:
    if not category_counts:
        return 0.0
    total = sum(category_counts.values())
    return category_counts.get(category, 0) / total


def _popularity(reward_id: str, popularity_counts: Counter, max_count: int) -> float:
    if max_count <= 0:
        return 0.0
    return popularity_counts.get(reward_id, 0) / max_count


def score_rewards_for_member(
    member: Member,
    candidate_rewards: list[RewardCatalogItem],
    member_redemption_categories: Counter,
    global_popularity: Counter,
) -> list[RewardScore]:
    """Pure function: given a member, eligible reward candidates, that
    member's historical redemption category counts, and global reward
    popularity counts -> ranked RewardScore list (descending)."""
    max_popularity = max(global_popularity.values()) if global_popularity else 0

    scored: list[RewardScore] = []
    for reward in candidate_rewards:
        if not reward.active:
            continue
        if TIER_RANK.get(member.tier, 0) < TIER_RANK.get(reward.tier_required, 0):
            continue

        affordability = _affordability_score(member, reward)
        affinity = _category_affinity(member_redemption_categories, reward.category)
        popularity = _popularity(reward.id, global_popularity, max_popularity)

        score = (
            WEIGHT_AFFORDABILITY * affordability
            + WEIGHT_AFFINITY * affinity
            + WEIGHT_POPULARITY * popularity
        )

        reasons = []
        if affordability >= 1.0:
            reasons.append("affordable now")
        elif affordability >= 0.7:
            reasons.append("close to affordable")
        if affinity > 0:
            reasons.append(f"matches past '{reward.category}' redemptions")
        if popularity >= 0.5:
            reasons.append("popular with other members")
        if not reasons:
            reasons.append("new option for this member")

        scored.append(RewardScore(reward=reward, score=round(score, 4), reason="; ".join(reasons)))

    scored.sort(key=lambda rs: rs.score, reverse=True)
    return scored


def _excluded_reward_ids_for_member(db: Session, member: Member) -> set[str]:
    """A/B testing (PLAN_BATCH3.md §5): Ledgerly has no separate
    member-facing storefront, so the concrete behavioral lever a reward
    experiment has is steering `recommend_for_member` -- if this member has
    an `ExperimentAssignment` for a still-`"running"` `RewardExperiment`,
    the *other* variant's reward is excluded from their recommendations so
    they only ever see/are steered toward their own assigned arm's version.
    Stops filtering once the experiment's status is no longer "running"
    (see `POST /experiments/{id}/end`)."""
    rows = (
        db.query(ExperimentAssignment.variant, RewardExperiment.variant_a_reward_id, RewardExperiment.variant_b_reward_id)
        .join(RewardExperiment, ExperimentAssignment.experiment_id == RewardExperiment.id)
        .filter(ExperimentAssignment.member_id == member.id, RewardExperiment.status == "running")
        .all()
    )
    excluded: set[str] = set()
    for variant, reward_a_id, reward_b_id in rows:
        excluded.add(reward_b_id if variant == "a" else reward_a_id)
    return excluded


def _candidate_rewards(db: Session, member: Member, excluded_reward_ids: set[str]) -> list[RewardCatalogItem]:
    candidate_query = db.query(RewardCatalogItem).filter(
        RewardCatalogItem.merchant_id == member.merchant_id, RewardCatalogItem.active.is_(True)
    )
    if excluded_reward_ids:
        candidate_query = candidate_query.filter(RewardCatalogItem.id.notin_(excluded_reward_ids))
    return candidate_query.all()


def _rank_candidates(db: Session, member: Member, candidates: list[RewardCatalogItem]) -> list[RewardScore]:
    member_redemptions = (
        db.query(Redemption)
        .filter(Redemption.member_id == member.id, Redemption.status == "completed")
        .all()
    )
    reward_by_id = {r.id: r for r in candidates}
    member_categories: Counter = Counter()
    for r in member_redemptions:
        reward = reward_by_id.get(r.reward_id)
        if reward:
            member_categories[reward.category] += 1

    all_completed_redemptions = (
        db.query(Redemption)
        .join(RewardCatalogItem, Redemption.reward_id == RewardCatalogItem.id)
        .filter(RewardCatalogItem.merchant_id == member.merchant_id, Redemption.status == "completed")
        .all()
    )
    global_popularity: Counter = Counter(r.reward_id for r in all_completed_redemptions)

    return score_rewards_for_member(member, candidates, member_categories, global_popularity)


def recommend_for_member(db: Session, member: Member, top_n: int = 5) -> list[RewardScore]:
    excluded_reward_ids = _excluded_reward_ids_for_member(db, member)

    ranked = _rank_candidates(db, member, _candidate_rewards(db, member, excluded_reward_ids))

    if not ranked and excluded_reward_ids:
        # A/B-experiment variant exclusion is best-effort steering, not a
        # hard business rule -- it must never leave a member with zero
        # recommendations when a non-empty list would otherwise be
        # available (TEST_REPORT_BATCH3.md §6: confirmed live-reproducible
        # with a small reward catalog where the excluded reward was the
        # member's only tier/active-eligible option). Fall back to not
        # excluding the other variant's reward for this call.
        ranked = _rank_candidates(db, member, _candidate_rewards(db, member, set()))

    return ranked[:top_n]
