"""Usage-based pricing (replaces the earlier per-member-count tier caps).

Ledgerly repositioned from "the loyalty program you already run" to "quick,
smart insight on the customer data you already have" -- a per-member price
no longer maps to anything real: two merchants with the same member count
can generate wildly different amounts of actual work (one uploads a CSV
once a quarter, another re-uploads weekly and pulls a fresh report after
every campaign). What Ledgerly actually costs to run scales with how often
a merchant asks it to turn data into insight, not how many rows sit in
their members table.

So each tier is now a flat monthly base fee that includes a number of
"insight runs" (see UsageEvent in app/db/models.py for exactly what counts
as one), plus a per-run rate for anything beyond that. This mirrors a
metered API pricing shape (OpenAI, Twilio, etc.) rather than a seat/cap
model.

Implementation note: this module tracks and displays usage entirely from
Ledgerly's own UsageEvent rows -- it does not yet report usage to Stripe as
metered billing (that needs a `stripe.SubscriptionItem.create_usage_record`
call against a real metered Price configured on Stripe's side, and this
environment has no real Stripe credentials to build or verify that against
-- see app/services/billing.py's module docstring for the same caveat on
the existing flat-tier checkout flow). Today, going over the included
allowance surfaces as an in-app nudge to upgrade (GET /billing/usage), not
an automatic overage charge. Wiring real Stripe metered usage records is a
follow-up once the owner has live Stripe keys and has configured metered
Price objects for `overage_price_gbp` per tier.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Merchant, UsageEvent

# One row per unit of billable work -- see UsageEvent's docstring for the
# exact two call sites that create these ("csv_upload" and
# "report_download"). Any kind added in future counts as a plain insight
# run for billing purposes; there is deliberately no per-kind pricing.
INSIGHT_RUN_KINDS = ("csv_upload", "report_download")


@dataclass(frozen=True)
class PlanDefinition:
    tier: str
    name: str
    base_price_gbp: float
    included_runs: int
    overage_price_gbp: float


# Single source of truth for pricing -- both GET /billing/plans (frontend
# render) and compute_usage_summary (overage estimate) read from this, so
# the two can't drift. Tier keys intentionally match
# app/services/billing.py's _TIER_PRICE_SETTINGS_ATTR / Stripe Price ID
# mapping -- this is the "what a tier means" layer, that module is "how to
# actually charge for it".
PLAN_DEFINITIONS: dict[str, PlanDefinition] = {
    "starter": PlanDefinition(
        tier="starter", name="Starter", base_price_gbp=29.0, included_runs=150, overage_price_gbp=0.30
    ),
    "growth": PlanDefinition(
        tier="growth", name="Growth", base_price_gbp=89.0, included_runs=750, overage_price_gbp=0.20
    ),
    "scale": PlanDefinition(
        tier="scale", name="Scale", base_price_gbp=249.0, included_runs=3000, overage_price_gbp=0.12
    ),
}

# Shown to a merchant who hasn't picked/paid for a tier yet (mid-trial) so
# GET /billing/usage still has something sensible to compare their usage
# against, rather than crashing or returning nulls.
TRIAL_PLAN = PLAN_DEFINITIONS["starter"]


def record_usage_event(db: Session, merchant: Merchant, kind: str) -> UsageEvent:
    """Records one billable insight run. Caller commits (same convention as
    every other write in this codebase -- see app/api/insights.py's call
    sites, which commit once at the end of the request alongside their own
    changes)."""
    if kind not in INSIGHT_RUN_KINDS:
        raise ValueError(f"unknown usage event kind: {kind!r}")
    event = UsageEvent(merchant_id=merchant.id, kind=kind)
    db.add(event)
    return event


def current_period_start(now: datetime | None = None) -> datetime:
    """Usage resets on a plain calendar-month boundary (UTC). Simpler and
    more predictable than trying to align with each merchant's actual
    Stripe billing anchor date, and doesn't require Stripe to be configured
    at all -- trial/dev merchants still get a meaningful usage count."""
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def count_insight_runs(db: Session, merchant_id: str, since: datetime) -> int:
    return (
        db.query(UsageEvent)
        .filter(UsageEvent.merchant_id == merchant_id, UsageEvent.created_at >= since)
        .count()
    )


@dataclass(frozen=True)
class UsageSummary:
    period_start: datetime
    plan: PlanDefinition
    insight_runs_used: int
    overage_runs: int
    estimated_overage_cost_gbp: float


def compute_usage_summary(db: Session, merchant: Merchant, now: datetime | None = None) -> UsageSummary:
    """Current calendar-month usage against the merchant's plan (or
    TRIAL_PLAN's allowance if they haven't subscribed to a tier yet)."""
    plan = PLAN_DEFINITIONS.get(merchant.subscription_tier or "", TRIAL_PLAN)
    period_start = current_period_start(now)
    used = count_insight_runs(db, merchant.id, period_start)
    overage_runs = max(0, used - plan.included_runs)
    return UsageSummary(
        period_start=period_start,
        plan=plan,
        insight_runs_used=used,
        overage_runs=overage_runs,
        estimated_overage_cost_gbp=round(overage_runs * plan.overage_price_gbp, 2),
    )
