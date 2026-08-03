"""Per-member predicted future value (PLAN_BATCH2.md §3).

Honest framing (read this before the code below): the seeded dataset is
~7,200 transactions over one continuous window for 620 members --
synthetic, single-period, with no repeated multi-cohort longitudinal
history and no ground-truth "actual future spend" label anyone collected
in the real world. A model claiming to be a rigorously trained,
cross-validated CLV regressor here would be overselling a demo. Following
this codebase's own established pattern (churn_model.py's docstring:
"Deliberately not a trained classifier at MVP scale... architected as a
scoring function with clearly named, swappable thresholds so a real
supervised model can replace this later"), this feature uses a
**backtested single-split regression with an explicit, documented
heuristic fallback** -- genuinely trained on scikit-learn against a real
(if narrow) label derived from the seeded data, not fabricated, but framed
honestly as an MVP proof-of-concept, not production CLV. Every response
says which path (`model_used`) produced it.

Backtest mechanics: pick cutoff = now - HOLDOUT_DAYS. For every member with
at least one earn transaction before cutoff, compute RFM-style features
using ONLY data before cutoff, and label them with `future_spend` = their
*actual* realized earn spend in [cutoff, cutoff + HOLDOUT_DAYS] (a real,
not-fabricated target -- it already happened). Fit
`sklearn.linear_model.Ridge` on that. At prediction time, every member's
features are recomputed from their entire available history (freshest
signal) and scored, scaled to the requested horizon.

Deliberate deviation from a literal reading of the plan: the plan's
feature-derivation prose says to reuse `churn_model.compute_rfm`'s "exact
recency/frequency/monetary definitions". Read literally that would mean
*calling* `compute_rfm(db, member, now=cutoff)` during training -- but
`compute_rfm` has no upper-bound time filter (its `window_start` is a
lower bound only, and its recency figure is always taken from
`member.last_activity_at`, which is the member's *true*, present-day last
activity, not their activity "as of" some earlier cutoff). Calling it with
`now=cutoff` at training time would leak post-cutoff transactions into the
supposedly pre-cutoff features -- exactly the kind of label leakage a
backtest exists to prevent. `_rfm_as_of` below reimplements the same
recency/frequency/monetary *formula* (same LOOKBACK_DAYS window, same
"earn transactions only" definition) but properly bounded to data with
`created_at <= as_of`, so the backtest is actually valid. At *prediction*
time (as_of=now, no cutoff involved) this function and `compute_rfm` are
equivalent, and `compute_rfm`/`score_member_churn` are called directly
where used (the heuristic fallback), exactly as the plan describes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split
from sqlalchemy.orm import Session

from app.ai.churn_model import LOOKBACK_DAYS, score_member_churn
from app.db.models import Member, TransactionType
from app.services.ledger import TIER_RANK

HOLDOUT_DAYS = 45  # backtest split: train on data before `now - HOLDOUT_DAYS`, label = realized spend in the HOLDOUT_DAYS after that
MIN_TRAINING_MEMBERS = 30  # below this, a fitted Ridge model would be statistically meaningless -- use the heuristic for everyone
RETENTION_DAMPING_MAX = 0.7  # heuristic: churn-risk damping never exceeds a 70% haircut, so a projection never zeroes out entirely

FEATURE_COLUMNS = ["recency_days", "frequency", "monetary", "avg_order_value", "tenure_days", "tier_rank"]


@dataclass
class FVFeatures:
    member_id: str
    recency_days: float
    frequency: int
    monetary: float
    avg_order_value: float
    tenure_days: float
    tier_rank: int

    def as_row(self) -> dict:
        return {
            "recency_days": self.recency_days,
            "frequency": self.frequency,
            "monetary": self.monetary,
            "avg_order_value": self.avg_order_value,
            "tenure_days": self.tenure_days,
            "tier_rank": self.tier_rank,
        }


@dataclass
class FutureValueModel:
    ridge: Ridge
    cutoff: datetime
    n_train: int
    r2: float | None  # None if the holdout split was too small to score meaningfully
    mae: float | None


@dataclass
class FutureValueResult:
    member_id: str
    first_name: str
    last_name: str
    predicted_value: float
    horizon_days: int
    model_used: Literal["trained", "heuristic"]
    avg_order_value: float
    monthly_purchase_rate: float


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _rfm_as_of(member: Member, as_of: datetime) -> tuple[float, int, float]:
    """Same recency/frequency/monetary definitions as
    `churn_model.compute_rfm`, but properly bounded to only transactions
    with `created_at <= as_of` -- see module docstring for why this can't
    just call `compute_rfm(now=as_of)` directly during training."""
    earn_and_redeem_before = [t for t in member.transactions if _aware(t.created_at) <= as_of]
    if earn_and_redeem_before:
        last_activity = max(_aware(t.created_at) for t in earn_and_redeem_before)
    else:
        last_activity = _aware(member.joined_at)
    recency_days = max(0.0, (as_of - last_activity).total_seconds() / 86400.0)

    window_start = as_of - timedelta(days=LOOKBACK_DAYS)
    earn_in_window = [
        t
        for t in member.transactions
        if t.type == TransactionType.EARN.value and window_start <= _aware(t.created_at) <= as_of
    ]
    frequency = len(earn_in_window)
    monetary = sum(t.amount_usd for t in earn_in_window)
    return recency_days, frequency, monetary


def compute_future_value_features(db: Session, member: Member, as_of: datetime) -> FVFeatures:
    """Pure function: member + as-of timestamp -> feature set. Used both at
    training time (as_of=cutoff, pre-cutoff-only data) and at prediction
    time (as_of=now, entire history)."""
    recency_days, frequency, monetary = _rfm_as_of(member, as_of)
    avg_order_value = monetary / max(frequency, 1)
    joined_at = _aware(member.joined_at)
    tenure_days = max(0.0, (as_of - joined_at).total_seconds() / 86400.0)
    tier_rank = TIER_RANK.get(member.tier, 0)
    return FVFeatures(
        member_id=member.id,
        recency_days=recency_days,
        frequency=frequency,
        monetary=monetary,
        avg_order_value=avg_order_value,
        tenure_days=tenure_days,
        tier_rank=tier_rank,
    )


def _has_pre_cutoff_activity(member: Member, cutoff: datetime) -> bool:
    return any(
        t.type == TransactionType.EARN.value and _aware(t.created_at) < cutoff for t in member.transactions
    )


def _future_spend_label(member: Member, cutoff: datetime, holdout_end: datetime) -> float:
    return sum(
        t.amount_usd
        for t in member.transactions
        if t.type == TransactionType.EARN.value and cutoff <= _aware(t.created_at) <= holdout_end
    )


def train_future_value_model(db: Session, merchant_id: str, now: datetime | None = None) -> FutureValueModel | None:
    """Backtest-train a Ridge regressor on this merchant's members. Returns
    None if fewer than MIN_TRAINING_MEMBERS are eligible (too few
    pre-cutoff-active members for training to be statistically meaningful)
    -- callers should use the heuristic for every member in that case."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=HOLDOUT_DAYS)

    members = db.query(Member).filter(Member.merchant_id == merchant_id).all()
    eligible = [m for m in members if _has_pre_cutoff_activity(m, cutoff)]
    if len(eligible) < MIN_TRAINING_MEMBERS:
        return None

    feature_rows = []
    labels = []
    for m in eligible:
        feats = compute_future_value_features(db, m, as_of=cutoff)
        feature_rows.append(feats.as_row())
        labels.append(_future_spend_label(m, cutoff, now))

    X = pd.DataFrame(feature_rows, columns=FEATURE_COLUMNS)
    y = pd.Series(labels)

    # Final production model: fit on ALL eligible members (plan step 2).
    ridge = Ridge(alpha=1.0, random_state=42)
    ridge.fit(X, y)

    # Separate train/test split purely to log R^2/MAE as a transparency
    # signal (plan step 2: "Log (not enforce)") -- not used to select or
    # gate the final model above.
    r2: float | None = None
    mae: float | None = None
    if len(eligible) >= 8:  # need a non-trivial holdout to compute a meaningful metric
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=42)
        if len(X_test) >= 2:
            metrics_model = Ridge(alpha=1.0, random_state=42)
            metrics_model.fit(X_train, y_train)
            preds = metrics_model.predict(X_test)
            r2 = round(float(r2_score(y_test, preds)), 4)
            mae = round(float(mean_absolute_error(y_test, preds)), 2)

    return FutureValueModel(ridge=ridge, cutoff=cutoff, n_train=len(eligible), r2=r2, mae=mae)


def predict_future_value(
    db: Session,
    member: Member,
    model: FutureValueModel | None,
    horizon_days: int = 90,
    now: datetime | None = None,
) -> FutureValueResult:
    """Score a single member. Falls back to the documented heuristic
    (per-member, not global) if `model` is None (merchant-wide: too few
    training members) or this specific member had zero pre-cutoff earn
    activity (too new to have been part of training) -- see plan step 4."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=HOLDOUT_DAYS)

    feats = compute_future_value_features(db, member, as_of=now)
    monthly_purchase_rate = feats.frequency / (LOOKBACK_DAYS / 30.0)

    use_trained = model is not None and _has_pre_cutoff_activity(member, cutoff)

    if use_trained:
        X = pd.DataFrame([feats.as_row()], columns=FEATURE_COLUMNS)
        raw_prediction = float(model.ridge.predict(X)[0])
        predicted_value = max(0.0, raw_prediction) * (horizon_days / HOLDOUT_DAYS)
        model_used: Literal["trained", "heuristic"] = "trained"
    else:
        churn = score_member_churn(db, member, now=now)
        retention_adjustment = 1 - (churn.churn_risk_score / 100.0) * RETENTION_DAMPING_MAX
        predicted_value = (
            feats.avg_order_value * monthly_purchase_rate * (horizon_days / 30.0) * retention_adjustment
        )
        predicted_value = max(0.0, predicted_value)
        model_used = "heuristic"

    return FutureValueResult(
        member_id=member.id,
        first_name=member.first_name,
        last_name=member.last_name,
        predicted_value=round(predicted_value, 2),
        horizon_days=horizon_days,
        model_used=model_used,
        avg_order_value=round(feats.avg_order_value, 2),
        monthly_purchase_rate=round(monthly_purchase_rate, 3),
    )


def score_all_members_future_value(
    db: Session, merchant_id: str, horizon_days: int = 90
) -> list[FutureValueResult]:
    """Trains once, reuses the same fitted model across every member --
    same "compute once per request, not per member" shape as
    fraud_detector.run_fraud_detection."""
    now = datetime.now(timezone.utc)
    members = db.query(Member).filter(Member.merchant_id == merchant_id).all()
    model = train_future_value_model(db, merchant_id, now=now)
    return [predict_future_value(db, m, model, horizon_days=horizon_days, now=now) for m in members]
