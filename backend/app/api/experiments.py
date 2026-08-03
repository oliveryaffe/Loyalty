"""A/B testing for reward structures (PLAN_BATCH3.md §5): create an
experiment (bulk-assigns every active member to variant A or B in one pass),
list/inspect experiments, view the results-comparison view, and end an
experiment.

Gated with `require_active_subscription`/`require_admin_active_subscription`
(not the older `get_current_merchant`), consistent with every other
paid-tier feature router in this batch -- same convention as
app/api/winback.py and app/api/settings.py, no additional tier-specific
enforcement beyond "has an active/trialing/past_due subscription" (the
Growth/Scale-tier positioning in the pricing table is a marketing/packaging
decision, not something this batch wires into API-level gating).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import require_active_subscription, require_admin_active_subscription
from app.db.base import get_db
from app.db.models import ExperimentAssignment, Merchant, RewardCatalogItem, RewardExperiment
from app.schemas.experiments import (
    ExperimentCreate,
    ExperimentDetailOut,
    ExperimentOut,
    ExperimentResultsOut,
    VariantResultOut,
)
from app.services.experiments import SAMPLE_SIZE_CAVEAT, bulk_assign_members, compute_results

router = APIRouter(prefix="/api/v1/experiments", tags=["experiments"])


def _get_experiment_or_404(db: Session, experiment_id: str, merchant_id: str) -> RewardExperiment:
    experiment = (
        db.query(RewardExperiment)
        .filter(RewardExperiment.id == experiment_id, RewardExperiment.merchant_id == merchant_id)
        .first()
    )
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return experiment


def _assignment_counts(db: Session, experiment_id: str) -> tuple[int, int]:
    rows = (
        db.query(ExperimentAssignment.variant)
        .filter(ExperimentAssignment.experiment_id == experiment_id)
        .all()
    )
    count_a = sum(1 for (variant,) in rows if variant == "a")
    count_b = sum(1 for (variant,) in rows if variant == "b")
    return count_a, count_b


@router.post("", response_model=ExperimentDetailOut, status_code=status.HTTP_201_CREATED)
def create_experiment(
    payload: ExperimentCreate,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> ExperimentDetailOut:
    """Create + bulk-assign (PLAN_BATCH3.md §5). 400 if either reward
    doesn't belong to this merchant, is inactive, or the two reward ids are
    identical."""
    if payload.variant_a_reward_id == payload.variant_b_reward_id:
        raise HTTPException(status_code=400, detail="variant_a_reward_id and variant_b_reward_id must differ")

    rewards = (
        db.query(RewardCatalogItem)
        .filter(
            RewardCatalogItem.id.in_([payload.variant_a_reward_id, payload.variant_b_reward_id]),
            RewardCatalogItem.merchant_id == merchant.id,
        )
        .all()
    )
    rewards_by_id = {r.id: r for r in rewards}
    variant_a_reward = rewards_by_id.get(payload.variant_a_reward_id)
    variant_b_reward = rewards_by_id.get(payload.variant_b_reward_id)
    if variant_a_reward is None or variant_b_reward is None:
        raise HTTPException(
            status_code=400, detail="Both reward ids must belong to this merchant"
        )
    if not variant_a_reward.active or not variant_b_reward.active:
        raise HTTPException(status_code=400, detail="Both rewards must be active")

    experiment = RewardExperiment(
        merchant_id=merchant.id,
        name=payload.name,
        variant_a_reward_id=payload.variant_a_reward_id,
        variant_b_reward_id=payload.variant_b_reward_id,
        traffic_split=payload.traffic_split,
    )
    db.add(experiment)
    db.flush()

    count_a, count_b = bulk_assign_members(db, experiment, merchant.id)

    db.commit()
    db.refresh(experiment)

    return ExperimentDetailOut(
        **ExperimentOut.model_validate(experiment).model_dump(),
        members_assigned_a=count_a,
        members_assigned_b=count_b,
    )


@router.get("", response_model=list[ExperimentOut])
def list_experiments(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> list[RewardExperiment]:
    return (
        db.query(RewardExperiment)
        .filter(RewardExperiment.merchant_id == merchant.id)
        .order_by(RewardExperiment.started_at.desc())
        .all()
    )


@router.get("/{experiment_id}", response_model=ExperimentDetailOut)
def get_experiment(
    experiment_id: str,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> ExperimentDetailOut:
    """Detail incl. assignment counts."""
    experiment = _get_experiment_or_404(db, experiment_id, merchant.id)
    count_a, count_b = _assignment_counts(db, experiment.id)
    return ExperimentDetailOut(
        **ExperimentOut.model_validate(experiment).model_dump(),
        members_assigned_a=count_a,
        members_assigned_b=count_b,
    )


@router.get("/{experiment_id}/results", response_model=ExperimentResultsOut)
def get_experiment_results(
    experiment_id: str,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> ExperimentResultsOut:
    """The comparison view (PLAN_BATCH3.md §5): per-variant
    members_assigned/redemptions_count/redemption_rate/total_points_spent,
    plus a hand-rolled two-proportion z-score and a directional_winner --
    explicitly framed as directional, not statistically rigorous
    (`sample_size_caveat`)."""
    experiment = _get_experiment_or_404(db, experiment_id, merchant.id)

    variant_a, variant_b, z_score, directional_winner = compute_results(db, experiment)

    reward_a = db.get(RewardCatalogItem, experiment.variant_a_reward_id)
    reward_b = db.get(RewardCatalogItem, experiment.variant_b_reward_id)

    return ExperimentResultsOut(
        experiment_id=experiment.id,
        status=experiment.status,
        variant_a=VariantResultOut(
            variant="a",
            reward_id=variant_a.reward_id,
            reward_name=reward_a.name if reward_a else "(deleted reward)",
            members_assigned=variant_a.members_assigned,
            redemptions_count=variant_a.redemptions_count,
            redemption_rate=variant_a.redemption_rate,
            total_points_spent=variant_a.total_points_spent,
        ),
        variant_b=VariantResultOut(
            variant="b",
            reward_id=variant_b.reward_id,
            reward_name=reward_b.name if reward_b else "(deleted reward)",
            members_assigned=variant_b.members_assigned,
            redemptions_count=variant_b.redemptions_count,
            redemption_rate=variant_b.redemption_rate,
            total_points_spent=variant_b.total_points_spent,
        ),
        z_score=z_score,
        directional_winner=directional_winner,
        sample_size_caveat=SAMPLE_SIZE_CAVEAT,
    )


@router.post("/{experiment_id}/end", response_model=ExperimentOut)
def end_experiment(
    experiment_id: str,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> RewardExperiment:
    """Sets status="completed", ended_at=now -- freezes the results framing
    as final; recommend_for_member stops steering by variant once
    status != "running".

    Idempotent: calling this on an already-completed experiment is a safe
    no-op that just returns the current (already-frozen) state, rather than
    re-stamping `ended_at` to the new call's timestamp -- a merchant/admin
    re-clicking "End experiment" (e.g. after a slow response) must not see
    the freeze-point silently move (TEST_REPORT_BATCH3.md §6)."""
    experiment = _get_experiment_or_404(db, experiment_id, merchant.id)
    if experiment.status == "completed":
        return experiment
    experiment.status = "completed"
    experiment.ended_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(experiment)
    return experiment
