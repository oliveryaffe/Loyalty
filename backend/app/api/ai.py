"""AI capability endpoints: recommendations, churn, fraud alerts."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.ai.churn_model import score_all_members, score_member_churn
from app.ai.fraud_detector import run_fraud_detection
from app.ai.recommender import recommend_for_member
from app.api.deps import get_current_merchant
from app.db.base import get_db
from app.db.models import FraudAlert, Member, Merchant
from app.schemas.ai import ChurnScoreOut, FraudAlertOut, RecommendationOut

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


@router.get("/recommendations/{member_id}", response_model=list[RecommendationOut])
def get_recommendations(
    member_id: str,
    top_n: int = 5,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(get_current_merchant),
) -> list[RecommendationOut]:
    member = (
        db.query(Member)
        .filter(Member.id == member_id, Member.merchant_id == merchant.id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")

    ranked = recommend_for_member(db, member, top_n=top_n)
    return [
        RecommendationOut(
            reward_id=rs.reward.id,
            reward_name=rs.reward.name,
            points_cost=rs.reward.points_cost,
            score=rs.score,
            reason=rs.reason,
        )
        for rs in ranked
    ]


@router.get("/churn", response_model=list[ChurnScoreOut])
def get_churn_scores(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(get_current_merchant),
) -> list[ChurnScoreOut]:
    results = score_all_members(db, merchant.id)
    return [
        ChurnScoreOut(
            member_id=r.member_id,
            first_name=r.first_name,
            last_name=r.last_name,
            recency_days=r.recency_days,
            frequency=r.frequency,
            monetary=r.monetary,
            churn_risk_score=r.churn_risk_score,
            risk_band=r.risk_band,
        )
        for r in results
    ]


@router.get("/churn/{member_id}", response_model=ChurnScoreOut)
def get_member_churn(
    member_id: str,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(get_current_merchant),
) -> ChurnScoreOut:
    member = (
        db.query(Member)
        .filter(Member.id == member_id, Member.merchant_id == merchant.id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    r = score_member_churn(db, member)
    return ChurnScoreOut(
        member_id=r.member_id,
        first_name=r.first_name,
        last_name=r.last_name,
        recency_days=r.recency_days,
        frequency=r.frequency,
        monetary=r.monetary,
        churn_risk_score=r.churn_risk_score,
        risk_band=r.risk_band,
    )


@router.get("/fraud-alerts", response_model=list[FraudAlertOut])
def get_fraud_alerts(
    refresh: bool = True,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(get_current_merchant),
) -> list[FraudAlert]:
    """List fraud alerts for this merchant. By default re-runs detection
    first (`refresh=True`) so the alert feed reflects the latest
    transactions; pass refresh=false to just read existing alerts."""
    if refresh:
        run_fraud_detection(db, merchant.id)
        db.commit()

    alerts = (
        db.query(FraudAlert)
        .join(Member, FraudAlert.member_id == Member.id)
        .filter(Member.merchant_id == merchant.id)
        .order_by(FraudAlert.score.desc())
        .all()
    )
    return alerts
