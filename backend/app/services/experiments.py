"""A/B testing for reward structures (PLAN_BATCH3.md §5) -- deliberately the
smallest-scope MVP shape the plan calls for: simple random assignment plus a
directional results-comparison view, not a full experimentation platform.

Ledgerly has no separate member-facing storefront -- `POST /rewards/redeem`
is called by merchant staff, not a shopper self-serving through a consumer
UI. So "assignment" here is a backend cohort split (which arm a member is
in), with two concrete effects: (1) `app/ai/recommender.py::recommend_for_member`
steers toward each member's assigned variant, and (2) results are measured
by comparing redemption behavior between the two cohorts.

Honest framing, matching this codebase's existing convention
(app/ai/future_value.py's "not a production CLV model" framing applied here
to statistics): at demo/MVP merchant scale, any observed redemption-rate
difference between variants will have wide confidence intervals. This ships
a directional comparison, not a rigorous significance test -- see
`SAMPLE_SIZE_CAVEAT`, surfaced verbatim on every results response so the
frontend can't accidentally present it as more conclusive than it is.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
from sqlalchemy.orm import Session

from app.db.models import ExperimentAssignment, Member, Redemption, RewardExperiment

SAMPLE_SIZE_CAVEAT = (
    "This is a directional comparison, not a statistically rigorous significance test. "
    "At typical merchant sample sizes, an observed difference between variants can easily "
    "be due to chance -- treat this as a signal worth investigating, not a conclusive verdict."
)

# Two-tailed ~95% cutoff. |z| below this -> "inconclusive" rather than
# picking a winner from noise.
Z_SIGNIFICANCE_THRESHOLD = 1.96


def assign_variant(experiment_id: str, member_id: str, traffic_split: float) -> str:
    """Deterministic, reproducible-without-persisting-a-seed random
    assignment (PLAN_BATCH3.md §5):
    `int(sha256(f"{experiment_id}:{member_id}").hexdigest(), 16) % 100 < traffic_split * 100`
    decides the arm. Because it's a pure function of (experiment_id,
    member_id, traffic_split) rather than `random.random()`, calling this
    again for the same pair always returns the same arm -- the "don't flip
    a member between requests" guarantee -- without needing a stored RNG
    state. In practice this function is only ever called once per member,
    at bulk-assignment time (see `bulk_assign_members`); after that, a
    member's arm is read back from the persisted `ExperimentAssignment` row,
    not recomputed."""
    digest = hashlib.sha256(f"{experiment_id}:{member_id}".encode("utf-8")).hexdigest()
    bucket = int(digest, 16) % 100
    return "b" if bucket < traffic_split * 100 else "a"


def bulk_assign_members(db: Session, experiment: RewardExperiment, merchant_id: str) -> tuple[int, int]:
    """Bulk, at-creation-time assignment (PLAN_BATCH3.md §5) -- simplest
    correct MVP interpretation of "simple random assignment". Every
    *active* member of the merchant at creation time gets an assignment in
    one pass; returns (count_assigned_to_a, count_assigned_to_b).

    Explicit MVP limitation (per the plan): members created *after* this
    call are NOT retroactively assigned -- they won't appear in either arm
    and won't be steered by recommend_for_member. No hook exists in member
    creation/CSV ingestion this batch to auto-assign late joiners."""
    members = (
        db.query(Member)
        .filter(Member.merchant_id == merchant_id, Member.is_active.is_(True))
        .all()
    )
    count_a = 0
    count_b = 0
    for member in members:
        variant = assign_variant(experiment.id, member.id, experiment.traffic_split)
        db.add(ExperimentAssignment(experiment_id=experiment.id, member_id=member.id, variant=variant))
        if variant == "a":
            count_a += 1
        else:
            count_b += 1
    db.flush()
    return count_a, count_b


@dataclass
class VariantResult:
    variant: str
    reward_id: str
    members_assigned: int
    redemptions_count: int
    redemption_rate: float
    total_points_spent: int


def _variant_result(db: Session, experiment_id: str, variant: str, reward_id: str) -> VariantResult:
    """Per-variant aggregate (PLAN_BATCH3.md §5's results view).

    `redemptions_count` / `redemption_rate` are counted by DISTINCT member
    (did this assigned member redeem the variant's reward at least once?),
    not by raw Redemption row count -- this keeps `redemption_rate` a true
    0..1 proportion (rather than something that could exceed 1.0 for a
    member who redeemed twice) and is also the statistically correct input
    to a two-proportion z-test (each assigned member is one independent
    trial). `total_points_spent` still sums every matching completed
    Redemption row, so repeat redemptions are fully reflected there even
    though they don't inflate `redemptions_count`.

    Joined against ExperimentAssignment so only *assigned* members' matching
    redemptions count -- a member outside the experiment who happens to
    redeem the same catalog reward must never inflate either variant's
    numbers (PLAN_BATCH3.md §5 acceptance criterion 4)."""
    assigned_member_ids = [
        row[0]
        for row in db.query(ExperimentAssignment.member_id)
        .filter(ExperimentAssignment.experiment_id == experiment_id, ExperimentAssignment.variant == variant)
        .all()
    ]
    members_assigned = len(assigned_member_ids)

    if not assigned_member_ids:
        return VariantResult(
            variant=variant,
            reward_id=reward_id,
            members_assigned=0,
            redemptions_count=0,
            redemption_rate=0.0,
            total_points_spent=0,
        )

    redemptions = (
        db.query(Redemption)
        .filter(
            Redemption.reward_id == reward_id,
            Redemption.member_id.in_(assigned_member_ids),
            Redemption.status == "completed",
        )
        .all()
    )
    redeemed_member_ids = {r.member_id for r in redemptions}
    redemptions_count = len(redeemed_member_ids)
    total_points_spent = sum(r.points_spent for r in redemptions)
    redemption_rate = redemptions_count / members_assigned if members_assigned else 0.0

    return VariantResult(
        variant=variant,
        reward_id=reward_id,
        members_assigned=members_assigned,
        redemptions_count=redemptions_count,
        redemption_rate=round(redemption_rate, 4),
        total_points_spent=total_points_spent,
    )


def _two_proportion_z_score(n_a: int, x_a: int, n_b: int, x_b: int) -> float | None:
    """Hand-rolled two-proportion z-test (PLAN_BATCH3.md §5: "a simple
    two-proportion z-score is computed by hand with numpy -- no new
    dependency, scipy isn't already a dependency and isn't worth adding for
    one calculation"). Positive z means B's redemption rate is higher than
    A's. Returns None if either arm has zero assigned members (nothing to
    compare)."""
    if n_a <= 0 or n_b <= 0:
        return None
    p_a = x_a / n_a
    p_b = x_b / n_b
    p_pool = (x_a + x_b) / (n_a + n_b)
    if p_pool <= 0.0 or p_pool >= 1.0:
        return 0.0
    se = float(np.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b)))
    if se == 0.0:
        return 0.0
    return round((p_b - p_a) / se, 4)


def compute_results(
    db: Session, experiment: RewardExperiment
) -> tuple[VariantResult, VariantResult, float | None, str]:
    """Returns (variant_a_result, variant_b_result, z_score, directional_winner).

    `directional_winner` is "inconclusive" whenever there isn't enough
    signal to call it (|z| < 1.96, or either arm has zero members) --
    deliberately conservative, matching the "directional, not rigorous"
    framing (PLAN_BATCH3.md §5)."""
    variant_a = _variant_result(db, experiment.id, "a", experiment.variant_a_reward_id)
    variant_b = _variant_result(db, experiment.id, "b", experiment.variant_b_reward_id)

    z_score = _two_proportion_z_score(
        variant_a.members_assigned,
        variant_a.redemptions_count,
        variant_b.members_assigned,
        variant_b.redemptions_count,
    )

    if z_score is None or abs(z_score) < Z_SIGNIFICANCE_THRESHOLD:
        directional_winner = "inconclusive"
    elif z_score > 0:
        directional_winner = "b"
    else:
        directional_winner = "a"

    return variant_a, variant_b, z_score, directional_winner
