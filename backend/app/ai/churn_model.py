"""Churn / attrition risk scoring (PLAN.md §3.2).

RFM (Recency / Frequency / Monetary) based scoring. Each member gets a
0-100 risk score where higher = more likely to disengage. Deliberately not
a trained classifier at MVP scale (no labeled churn outcome exists yet) --
architected as a scoring function with clearly named, swappable thresholds
so a real supervised model can replace `churn_risk_from_rfm` later without
touching the API layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Member, Transaction, TransactionType

# Tuning constants -- see docstring above re: swappability.
RECENCY_SATURATION_DAYS = 90.0  # inactivity at/beyond this = max recency risk
FREQUENCY_SATURATION = 8.0  # earn-transactions in lookback window for zero freq risk
MONETARY_SATURATION = 400.0  # £ spent in lookback window for zero monetary risk
LOOKBACK_DAYS = 120  # window used for frequency/monetary components

WEIGHT_RECENCY = 0.5
WEIGHT_FREQUENCY = 0.3
WEIGHT_MONETARY = 0.2

RISK_BAND_LOW_MAX = 35.0
RISK_BAND_MEDIUM_MAX = 65.0


@dataclass
class ChurnResult:
    member_id: str
    first_name: str
    last_name: str
    recency_days: float
    frequency: int
    monetary: float
    churn_risk_score: float
    risk_band: str


def _risk_band(score: float) -> str:
    if score < RISK_BAND_LOW_MAX:
        return "low"
    if score < RISK_BAND_MEDIUM_MAX:
        return "medium"
    return "high"


def churn_risk_from_rfm(recency_days: float, frequency: int, monetary: float) -> float:
    """Pure function: RFM features -> 0-100 risk score. Higher = riskier."""
    recency_risk = min(100.0, (recency_days / RECENCY_SATURATION_DAYS) * 100.0)
    frequency_risk = max(0.0, 100.0 - (frequency / FREQUENCY_SATURATION) * 100.0)
    frequency_risk = min(100.0, frequency_risk)
    monetary_risk = max(0.0, 100.0 - (monetary / MONETARY_SATURATION) * 100.0)
    monetary_risk = min(100.0, monetary_risk)

    score = (
        WEIGHT_RECENCY * recency_risk
        + WEIGHT_FREQUENCY * frequency_risk
        + WEIGHT_MONETARY * monetary_risk
    )
    return round(max(0.0, min(100.0, score)), 2)


def compute_rfm(db: Session, member: Member, now: datetime | None = None) -> tuple[float, int, float]:
    """Compute (recency_days, frequency, monetary) for a member.

    Frequency/monetary are counted over the trailing LOOKBACK_DAYS window of
    *earn* transactions (purchases); recency is days since last activity of
    any kind (earn or redeem).
    """
    now = now or datetime.now(timezone.utc)

    last_activity = member.last_activity_at
    if last_activity.tzinfo is None:
        last_activity = last_activity.replace(tzinfo=timezone.utc)
    recency_days = max(0.0, (now - last_activity).total_seconds() / 86400.0)

    window_start = now.timestamp() - LOOKBACK_DAYS * 86400.0

    earn_txns = [
        t
        for t in member.transactions
        if t.type == TransactionType.EARN.value
        and (
            t.created_at.replace(tzinfo=timezone.utc) if t.created_at.tzinfo is None else t.created_at
        ).timestamp()
        >= window_start
    ]
    frequency = len(earn_txns)
    monetary = sum(t.amount_gbp for t in earn_txns)

    return recency_days, frequency, monetary


def score_member_churn(db: Session, member: Member, now: datetime | None = None) -> ChurnResult:
    recency_days, frequency, monetary = compute_rfm(db, member, now=now)
    score = churn_risk_from_rfm(recency_days, frequency, monetary)
    return ChurnResult(
        member_id=member.id,
        first_name=member.first_name,
        last_name=member.last_name,
        recency_days=round(recency_days, 1),
        frequency=frequency,
        monetary=round(monetary, 2),
        churn_risk_score=score,
        risk_band=_risk_band(score),
    )


def score_all_members(db: Session, merchant_id: str, now: datetime | None = None) -> list[ChurnResult]:
    members = db.query(Member).filter(Member.merchant_id == merchant_id).all()
    return [score_member_churn(db, m, now=now) for m in members]
