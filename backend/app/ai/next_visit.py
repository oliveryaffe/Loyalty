"""Predicted next visit/order date per member (competitor research
finding: Klaviyo surfaces a "predicted order date" per contact alongside
churn risk and CLV -- this is the equivalent, computed entirely from the
same purchase-history data this AI layer already reads, no new model or
external dependency).

Method: a member's own median gap between consecutive earn transactions
is used once there are enough of their own purchases to trust it
(MIN_OWN_PURCHASES_FOR_MEMBER_RHYTHM); below that, this merchant's
own observed median inter-purchase gap across its whole member base is
used instead (same "measure this merchant's real rhythm, don't assume a
fixed number" principle as churn_model.py's compute_merchant_calibration
-- deliberately NOT derived algebraically from MerchantCalibration's
fields, since lookback_days/recency_saturation_days are independently
tuned per vertical and don't reduce to a single "typical interval" via a
fixed ratio, as verified against BUSINESS_TYPE_CALIBRATIONS's retail
profile: lookback_days/LOOKBACK_CYCLES badly underestimates its true
~quarterly cycle because lookback_days gets clamped at MAX_LOOKBACK_DAYS
for long-cycle verticals rather than scaling freely).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.db.models import Member, TransactionType

# A member's own median needs at least 2 gaps (3 purchases) before it's
# trusted over the merchant-wide fallback -- same "don't trust a noisy
# n" principle as churn_model.py's MIN_MEMBERS_WITH_REPEAT_VISITS.
MIN_OWN_PURCHASES_FOR_MEMBER_RHYTHM = 3

# Below this many merchant-wide observed gaps, there isn't a reliable
# fallback either (e.g. a brand-new account with almost no repeat
# purchases yet) -- predictions stay "insufficient_data" rather than
# guessing off a handful of data points.
MIN_MERCHANT_GAPS_FOR_FALLBACK = 5


@dataclass(frozen=True)
class NextVisitPrediction:
    member_id: str
    predicted_next_visit_date: date | None
    typical_interval_days: float | None
    source: Literal["member", "merchant", "insufficient_data"]
    # Positive => this many days past the predicted date with no visit
    # yet ("overdue"); None if not predictable or not yet due.
    days_overdue: int | None


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _sorted_earn_dates(member: Member) -> list[datetime]:
    dates = [_aware(t.created_at) for t in member.transactions if t.type == TransactionType.EARN.value]
    return sorted(dates)


def _median_gap_days(dates: list[datetime]) -> float | None:
    if len(dates) < 2:
        return None
    gaps = [(b - a).total_seconds() / 86400.0 for a, b in zip(dates, dates[1:])]
    return statistics.median(gaps)


def _merchant_wide_typical_interval(members: list[Member]) -> float | None:
    all_gaps: list[float] = []
    for m in members:
        dates = _sorted_earn_dates(m)
        if len(dates) < 2:
            continue
        all_gaps.extend((b - a).total_seconds() / 86400.0 for a, b in zip(dates, dates[1:]))
    if len(all_gaps) < MIN_MERCHANT_GAPS_FOR_FALLBACK:
        return None
    return statistics.median(all_gaps)


def predict_next_visit(
    member: Member,
    merchant_typical_interval_days: float | None,
    now: datetime | None = None,
) -> NextVisitPrediction:
    now = now or datetime.now(timezone.utc)
    dates = _sorted_earn_dates(member)

    interval: float | None
    source: Literal["member", "merchant", "insufficient_data"]

    if len(dates) >= MIN_OWN_PURCHASES_FOR_MEMBER_RHYTHM:
        interval = _median_gap_days(dates)
        source = "member"
    elif dates and merchant_typical_interval_days is not None:
        interval = merchant_typical_interval_days
        source = "merchant"
    else:
        interval = None
        source = "insufficient_data"

    if interval is None or not dates:
        return NextVisitPrediction(
            member_id=member.id,
            predicted_next_visit_date=None,
            typical_interval_days=None,
            source="insufficient_data",
            days_overdue=None,
        )

    last_visit = dates[-1]
    predicted = last_visit + timedelta(days=interval)
    overdue_days = int((now - predicted).total_seconds() / 86400.0)

    return NextVisitPrediction(
        member_id=member.id,
        predicted_next_visit_date=predicted.date(),
        typical_interval_days=round(interval, 1),
        source=source,
        days_overdue=overdue_days if overdue_days > 0 else None,
    )


def predict_next_visit_for_all_members(db: Session, merchant_id: str) -> list[NextVisitPrediction]:
    """Batch entry point -- computes the merchant-wide fallback interval
    once, same "compute once per request, not once per member" shape as
    fraud_detector.run_fraud_detection / future_value's
    score_all_members_future_value."""
    members = db.query(Member).filter(Member.merchant_id == merchant_id).all()
    merchant_typical = _merchant_wide_typical_interval(members)
    now = datetime.now(timezone.utc)
    return [predict_next_visit(m, merchant_typical, now=now) for m in members]
