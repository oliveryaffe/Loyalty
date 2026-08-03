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
