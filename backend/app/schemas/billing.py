"""Stripe billing schemas (PLAN_BATCH3.md §2)."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

SubscriptionTier = Literal["starter", "growth", "scale"]


class CheckoutSessionRequest(BaseModel):
    tier: SubscriptionTier


class CheckoutSessionOut(BaseModel):
    checkout_url: str


class PortalSessionOut(BaseModel):
    portal_url: str


class SubscriptionOut(BaseModel):
    """Powers the dashboard's billing banner/settings page -- current
    status/tier/period-end/trial-end. `subscription_status`/`tier` are
    `None` for a merchant that has never subscribed."""

    subscription_status: str | None
    subscription_tier: str | None
    subscription_current_period_end: datetime | None
    trial_ends_at: datetime | None


class PlanOut(BaseModel):
    """One row of the usage-based pricing table (app/services/usage.py) --
    a flat monthly base fee that includes a number of "insight runs" per
    month (a CSV upload processed or a report exported -- see UsageEvent),
    plus a per-run rate for anything beyond that. Replaces the old
    per-member-count tier caps."""

    tier: SubscriptionTier
    name: str
    base_price_gbp: float
    included_runs: int
    overage_price_gbp: float


class UsageOut(BaseModel):
    """Current calendar-month insight-run usage against the merchant's
    plan allowance (app/services/usage.py::compute_usage_summary).
    `estimated_overage_cost_gbp` is informational only -- it is not yet
    charged automatically (see usage.py's module docstring)."""

    period_start: datetime
    tier: SubscriptionTier
    plan_name: str
    included_runs: int
    insight_runs_used: int
    overage_runs: int
    estimated_overage_cost_gbp: float
