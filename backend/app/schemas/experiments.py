"""A/B testing schemas (PLAN_BATCH3.md §5)."""
from datetime import datetime

from pydantic import BaseModel, Field


class ExperimentCreate(BaseModel):
    name: str
    variant_a_reward_id: str
    variant_b_reward_id: str
    # Fraction of assigned members steered to variant B. Bounded to a valid
    # 0.0-1.0 split ratio -- `assign_variant`'s bucket check
    # (`bucket < traffic_split * 100`, bucket in 0..99) is unsatisfiable for
    # negative values (100% silently to "a") and always-true for values
    # >= 1.0 (100% silently to "b"); previously only the frontend slider
    # ([0.05, 0.95]) prevented this, which left the raw API contract open to
    # a degenerate, fully-lopsided experiment via any direct caller
    # (TEST_REPORT_BATCH3.md §6).
    traffic_split: float = Field(default=0.5, ge=0.0, le=1.0)


class ExperimentOut(BaseModel):
    id: str
    merchant_id: str
    name: str
    variant_a_reward_id: str
    variant_b_reward_id: str
    traffic_split: float
    status: str
    started_at: datetime
    ended_at: datetime | None = None

    class Config:
        from_attributes = True


class ExperimentDetailOut(ExperimentOut):
    members_assigned_a: int
    members_assigned_b: int


class ExperimentAssignmentOut(BaseModel):
    """A single member's A/B-test cohort assignment -- also surfaced in the
    GDPR export (app/schemas/gdpr.py::MemberExportOut), since which arm a
    named individual was placed in is personal data about them."""

    id: str
    experiment_id: str
    member_id: str
    variant: str  # "a" | "b"
    assigned_at: datetime

    class Config:
        from_attributes = True


class VariantResultOut(BaseModel):
    variant: str  # "a" | "b"
    reward_id: str
    reward_name: str
    members_assigned: int
    redemptions_count: int
    redemption_rate: float
    total_points_spent: int


class ExperimentResultsOut(BaseModel):
    experiment_id: str
    status: str
    variant_a: VariantResultOut
    variant_b: VariantResultOut
    z_score: float | None
    directional_winner: str  # "a" | "b" | "inconclusive"
    sample_size_caveat: str
