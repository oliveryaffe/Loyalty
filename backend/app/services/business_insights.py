"""Business-level "so what" insight reporting (competitive-brief follow-up:
per-customer churn/future-value/next-best-product scores were reported as
missing a business-level "so what" -- a merchant admin has to read 600+
individual rows to form any opinion about their own business). This module
adds no new scoring model; it aggregates the churn/future-value models
that already exist into four things a shop owner can actually act on:

1. Revenue-at-risk headline -- ties churn risk and future value together
   into one number: how much of the predicted book sits in customers
   currently flagged high-risk.
2. Trend -- whether that's getting better or worse since the last time it
   was measured (see MerchantMetricSnapshot in app/db/models.py -- this is
   the only place in the app that stores a metric over time rather than
   recomputing it fresh on every request).
3. Category performance -- which reward category (or, for merchants who
   don't run a rewards programme -- see Rewards.tsx's copy -- which
   purchase category) is actually associated with higher-value customers,
   not just a per-member "next best" guess.
4. Churn driver -- the dominant reason across the *whole* at-risk cohort,
   not just a per-member explanation (app/ai/churn_model.py::
   explain_churn_risk already does the per-member version of this; this
   is the aggregate version).
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.ai.churn_model import (
    WEIGHT_FREQUENCY,
    WEIGHT_MONETARY,
    WEIGHT_RECENCY,
    ChurnResult,
    MerchantCalibration,
    _risk_components,
    compute_merchant_calibration,
    score_all_members,
)
from app.ai.future_value import score_all_members_future_value
from app.db.models import (
    Member,
    Merchant,
    MerchantMetricSnapshot,
    Redemption,
    RedemptionStatus,
    RewardCatalogItem,
    Transaction,
    TransactionType,
)

# Don't capture a new snapshot more than once every N days -- repeatedly
# loading the Insights page shouldn't spam rows, and a trend line is only
# meaningful measured across a real stretch of time anyway.
SNAPSHOT_MIN_INTERVAL_DAYS = 7

# A category needs at least this many engaged members before its average
# future value is reported -- below this, it's noise, not a pattern.
MIN_MEMBERS_FOR_CATEGORY_STAT = 3
MAX_CATEGORIES_SHOWN = 3


@dataclass(frozen=True)
class RevenueAtRisk:
    total_future_value_gbp: float
    at_risk_future_value_gbp: float
    at_risk_share: float | None  # None when there's no future-value data yet
    headline: str


@dataclass(frozen=True)
class TrendSummary:
    previous_captured_at: datetime
    days_since_previous: int
    high_risk_count_delta: int
    at_risk_future_value_gbp_delta: float
    headline: str


@dataclass(frozen=True)
class ChurnDriverSummary:
    dominant_driver: Literal["recency", "frequency", "monetary"] | None
    share_of_high_risk: float
    headline: str


@dataclass(frozen=True)
class CategoryPerformance:
    category: str
    source: Literal["redemption", "purchase"]
    engaged_members: int
    avg_future_value_gbp: float
    lift_pct: float


@dataclass(frozen=True)
class BusinessInsightsReport:
    generated_at: datetime
    total_members: int
    revenue_at_risk: RevenueAtRisk
    trend: TrendSummary | None
    churn_driver: ChurnDriverSummary | None
    top_categories: list[CategoryPerformance]


def _compute_revenue_at_risk(
    churn_results: list[ChurnResult], future_value_by_member_id: dict[str, float]
) -> RevenueAtRisk:
    total = sum(future_value_by_member_id.values())
    high_risk_ids = {r.member_id for r in churn_results if r.risk_band == "high"}
    at_risk = sum(v for member_id, v in future_value_by_member_id.items() if member_id in high_risk_ids)

    if total <= 0:
        return RevenueAtRisk(
            total_future_value_gbp=0.0,
            at_risk_future_value_gbp=0.0,
            at_risk_share=None,
            headline="Not enough transaction history yet to estimate predicted value or revenue at risk.",
        )

    share = at_risk / total
    if at_risk <= 0:
        headline = (
            f"£{total:,.2f} of predicted 90-day value across your book -- none of it currently sits "
            f"with a high-risk customer."
        )
    else:
        headline = (
            f"£{at_risk:,.2f} of your predicted 90-day value ({share * 100:.0f}%) sits in customers "
            f"currently flagged high-risk."
        )
    return RevenueAtRisk(
        total_future_value_gbp=round(total, 2),
        at_risk_future_value_gbp=round(at_risk, 2),
        at_risk_share=round(share, 4),
        headline=headline,
    )


def _latest_snapshot(db: Session, merchant_id: str) -> MerchantMetricSnapshot | None:
    return (
        db.query(MerchantMetricSnapshot)
        .filter(MerchantMetricSnapshot.merchant_id == merchant_id)
        .order_by(MerchantMetricSnapshot.captured_at.desc())
        .first()
    )


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _build_trend(
    previous: MerchantMetricSnapshot | None,
    now: datetime,
    high_risk_count: int,
    at_risk_future_value_gbp: float,
) -> TrendSummary | None:
    if previous is None:
        return None

    captured_at = _aware(previous.captured_at)
    days_since = int(round((now - captured_at).total_seconds() / 86400.0))
    if days_since < 1:
        return None  # too soon since the last snapshot to say anything meaningful

    high_risk_delta = high_risk_count - previous.high_risk_count
    value_delta = round(at_risk_future_value_gbp - previous.at_risk_future_value_gbp, 2)

    if high_risk_delta == 0:
        risk_phrase = "unchanged"
    elif high_risk_delta > 0:
        risk_phrase = f"up {high_risk_delta}"
    else:
        risk_phrase = f"down {abs(high_risk_delta)}"

    headline = (
        f"Since {days_since} day(s) ago: high-risk customers {risk_phrase} "
        f"({previous.high_risk_count} → {high_risk_count})."
    )

    return TrendSummary(
        previous_captured_at=captured_at,
        days_since_previous=days_since,
        high_risk_count_delta=high_risk_delta,
        at_risk_future_value_gbp_delta=value_delta,
        headline=headline,
    )


def _maybe_capture_snapshot(
    db: Session,
    merchant_id: str,
    now: datetime,
    previous: MerchantMetricSnapshot | None,
    total_members: int,
    high_risk_count: int,
    medium_risk_count: int,
    low_risk_count: int,
    total_future_value_gbp: float,
    at_risk_future_value_gbp: float,
) -> None:
    if previous is not None:
        if now - _aware(previous.captured_at) < timedelta(days=SNAPSHOT_MIN_INTERVAL_DAYS):
            return
    db.add(
        MerchantMetricSnapshot(
            merchant_id=merchant_id,
            captured_at=now,
            total_members=total_members,
            high_risk_count=high_risk_count,
            medium_risk_count=medium_risk_count,
            low_risk_count=low_risk_count,
            total_future_value_gbp=total_future_value_gbp,
            at_risk_future_value_gbp=at_risk_future_value_gbp,
        )
    )
    db.flush()


def _compute_churn_driver_summary(
    churn_results: list[ChurnResult], calibration: MerchantCalibration
) -> ChurnDriverSummary | None:
    high_risk = [r for r in churn_results if r.risk_band == "high"]
    if not high_risk:
        return None

    driver_counts: Counter[str] = Counter()
    recency_values: list[float] = []

    for r in high_risk:
        recency_risk, frequency_risk, monetary_risk = _risk_components(
            r.recency_days, r.frequency, r.monetary, calibration
        )
        contributions = {
            "recency": WEIGHT_RECENCY * recency_risk,
            "frequency": WEIGHT_FREQUENCY * frequency_risk,
            "monetary": WEIGHT_MONETARY * monetary_risk,
        }
        driver = max(contributions, key=lambda k: contributions[k])
        driver_counts[driver] += 1
        if driver == "recency":
            recency_values.append(r.recency_days)

    dominant, count = driver_counts.most_common(1)[0]
    share = count / len(high_risk)

    if dominant == "recency" and recency_values:
        median_recency = statistics.median(recency_values)
        headline = (
            f"{share * 100:.0f}% of your at-risk customers are flagged for the same reason: they haven't "
            f"been back in a while -- typically around {median_recency:.0f} days, well past this "
            f"account's usual rhythm."
        )
    elif dominant == "frequency":
        headline = (
            f"{share * 100:.0f}% of your at-risk customers are flagged mainly for visiting less often "
            f"than usual, not for staying away entirely -- a nudge to come back sooner may work better "
            f"than a win-back discount."
        )
    else:
        headline = (
            f"{share * 100:.0f}% of your at-risk customers are flagged mainly for spending less per "
            f"visit than usual, not for visiting less often."
        )

    return ChurnDriverSummary(dominant_driver=dominant, share_of_high_risk=round(share, 2), headline=headline)


def _compute_category_performance(
    db: Session, merchant_id: str, future_value_by_member_id: dict[str, float]
) -> list[CategoryPerformance]:
    if not future_value_by_member_id:
        return []
    baseline = statistics.mean(future_value_by_member_id.values())
    if baseline <= 0:
        return []

    by_category: dict[str, set[str]] = defaultdict(set)
    source: Literal["redemption", "purchase"] = "redemption"

    redemption_rows = (
        db.query(Redemption.member_id, RewardCatalogItem.category)
        .join(RewardCatalogItem, Redemption.reward_id == RewardCatalogItem.id)
        .join(Member, Redemption.member_id == Member.id)
        .filter(Member.merchant_id == merchant_id, Redemption.status == RedemptionStatus.COMPLETED.value)
        .all()
    )
    for member_id, category in redemption_rows:
        by_category[category].add(member_id)

    if not by_category:
        # No reward catalog / no redemptions -- fall back to purchase
        # category (Transaction.product_category), same fallback
        # relationship as next_best_product.py's category granularity.
        source = "purchase"
        txn_rows = (
            db.query(Transaction.member_id, Transaction.product_category)
            .join(Member, Transaction.member_id == Member.id)
            .filter(
                Member.merchant_id == merchant_id,
                Transaction.type == TransactionType.EARN.value,
                Transaction.product_category.isnot(None),
            )
            .all()
        )
        for member_id, category in txn_rows:
            by_category[category].add(member_id)

    rows: list[CategoryPerformance] = []
    for category, member_ids in by_category.items():
        engaged_values = [future_value_by_member_id[m] for m in member_ids if m in future_value_by_member_id]
        if len(engaged_values) < MIN_MEMBERS_FOR_CATEGORY_STAT:
            continue
        avg = statistics.mean(engaged_values)
        lift_pct = ((avg / baseline) - 1.0) * 100.0
        rows.append(
            CategoryPerformance(
                category=category,
                source=source,
                engaged_members=len(engaged_values),
                avg_future_value_gbp=round(avg, 2),
                lift_pct=round(lift_pct, 1),
            )
        )

    rows.sort(key=lambda r: r.lift_pct, reverse=True)
    return rows[:MAX_CATEGORIES_SHOWN]


def compute_business_insights(db: Session, merchant: Merchant) -> BusinessInsightsReport:
    """Main entry point -- called from GET /insights/business-insights.
    Reuses the same churn/future-value computation every other endpoint
    already runs (no new modeling), then layers the four aggregate views
    on top. Also opportunistically captures a MerchantMetricSnapshot row
    (see that model's docstring) so a future call can build a trend."""
    now = datetime.now(timezone.utc)
    calibration = compute_merchant_calibration(db, merchant.id, now=now)
    churn_results = score_all_members(db, merchant.id, now=now, calibration=calibration)
    future_value_results = score_all_members_future_value(db, merchant.id)
    future_value_by_member_id = {r.member_id: r.predicted_value for r in future_value_results}

    total_members = len(churn_results)
    high_risk_count = sum(1 for r in churn_results if r.risk_band == "high")
    medium_risk_count = sum(1 for r in churn_results if r.risk_band == "medium")
    low_risk_count = total_members - high_risk_count - medium_risk_count

    revenue_at_risk = _compute_revenue_at_risk(churn_results, future_value_by_member_id)

    previous_snapshot = _latest_snapshot(db, merchant.id)
    trend = _build_trend(previous_snapshot, now, high_risk_count, revenue_at_risk.at_risk_future_value_gbp)
    _maybe_capture_snapshot(
        db,
        merchant.id,
        now,
        previous_snapshot,
        total_members,
        high_risk_count,
        medium_risk_count,
        low_risk_count,
        revenue_at_risk.total_future_value_gbp,
        revenue_at_risk.at_risk_future_value_gbp,
    )

    churn_driver = _compute_churn_driver_summary(churn_results, calibration)
    top_categories = _compute_category_performance(db, merchant.id, future_value_by_member_id)

    return BusinessInsightsReport(
        generated_at=now,
        total_members=total_members,
        revenue_at_risk=revenue_at_risk,
        trend=trend,
        churn_driver=churn_driver,
        top_categories=top_categories,
    )
