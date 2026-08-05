"""AI capability endpoints: recommendations, churn, fraud alerts.

PLAN_BATCH3.md §3: `GET /churn` and `GET /fraud-alerts` are also where
notifications (Slack/email on churn escalation + new fraud alerts) are
wired in -- there is no scheduler in this codebase, so notification
delivery piggybacks on these existing request-triggered recompute paths
(see app/services/notifications.py for the full rationale). Delivery
itself happens via FastAPI `BackgroundTasks` so a slow/down Slack endpoint
never delays these responses. Win-back (app/services/winback.py) is a
separate, read-only worklist computed on demand at GET /winback/worklist
-- it doesn't hook into this recompute path at all anymore.

Same reasoning extends the weekly digest (app/services/digest.py):
`get_churn_scores` also checks whether a merchant's digest is due (opted
in + 7+ days since the last send) and fires it as a background task if
so -- the dashboard load that already happens is the only "trigger" this
passive feature needs.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.ai.churn_model import compute_merchant_calibration, score_all_members, score_member_churn
from app.ai.fraud_detector import explain_fraud_finding, run_fraud_detection
from app.ai.recommender import recommend_for_member
from app.api.deps import require_active_subscription
from app.db.base import get_db
from app.db.models import FraudAlert, Member, Merchant
from app.schemas.ai import ChurnScoreOut, FraudAlertOut, RecommendationOut
from app.services.digest import compute_weekly_digest, format_digest_email, is_digest_due, wants_weekly_digest
from app.services.winback import get_suggested_winback_reward
from app.services.notifications import (
    check_churn_escalations,
    format_member_bullet_list,
    notify_merchant,
    wants_churn_notifications,
    wants_fraud_notifications,
)
from app.services.usage import record_usage_event
router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


@router.get("/recommendations/{member_id}", response_model=list[RecommendationOut])
def get_recommendations(
    member_id: str,
    top_n: int = 5,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
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
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> list[ChurnScoreOut]:
    results = score_all_members(db, merchant.id)

    # PLAN_BATCH3.md §3: transition-detect newly-escalated-to-high members
    # (dedup/cooldown handled inside check_churn_escalations) and, if any,
    # notify (batched, one message per request). Win-back no longer has
    # an auto-trigger path (see app/services/winback.py) -- it's now a
    # read-only worklist computed on demand at GET /winback/worklist.
    escalated = check_churn_escalations(db, merchant, results)
    if escalated and wants_churn_notifications(merchant):
        subject = f"{len(escalated)} member(s) just escalated to high churn risk"
        # Enriches the alert with the merchant's saved win-back reward
        # suggestion (competitor research: Zinrelo/Antavo pair an at-risk
        # alert with a recommended incentive). Still read-only/suggestion
        # -- nothing is sent to the member and nothing is granted, same
        # "Ledgerly isn't the system of record" boundary as
        # app/services/winback.py itself; this just puts the suggestion
        # in front of the person who'll act on it a few seconds sooner.
        suggested_reward = get_suggested_winback_reward(db, merchant)
        names = [f"{m.first_name} {m.last_name}" for m in escalated]
        if suggested_reward is not None:
            names = [f"{name} -- suggested offer: {suggested_reward.name}" for name in names]
        body = format_member_bullet_list(names)
        notify_merchant(merchant, subject, body, background_tasks)

    if wants_weekly_digest(merchant) and is_digest_due(merchant):
        digest = compute_weekly_digest(db, merchant)
        digest_subject, digest_body = format_digest_email(digest, merchant.business_name)
        notify_merchant(merchant, digest_subject, digest_body, background_tasks)
        merchant.last_digest_sent_at = datetime.now(timezone.utc)
        record_usage_event(db, merchant, "weekly_digest")

    db.commit()

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
            explanation=r.explanation,
        )
        for r in results
    ]


@router.get("/churn/{member_id}", response_model=ChurnScoreOut)
def get_member_churn(
    member_id: str,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> ChurnScoreOut:
    member = (
        db.query(Member)
        .filter(Member.id == member_id, Member.merchant_id == merchant.id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    calibration = compute_merchant_calibration(db, merchant.id)
    r = score_member_churn(db, member, calibration=calibration)
    return ChurnScoreOut(
        member_id=r.member_id,
        first_name=r.first_name,
        last_name=r.last_name,
        recency_days=r.recency_days,
        frequency=r.frequency,
        monetary=r.monetary,
        churn_risk_score=r.churn_risk_score,
        risk_band=r.risk_band,
        explanation=r.explanation,
    )


@router.get("/fraud-alerts", response_model=list[FraudAlertOut])
def get_fraud_alerts(
    background_tasks: BackgroundTasks,
    refresh: bool = True,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> list[FraudAlert]:
    """List fraud alerts for this merchant. By default re-runs detection
    first (`refresh=True`) so the alert feed reflects the latest
    transactions; pass refresh=false to just read existing alerts.

    PLAN_BATCH3.md §3: run_fraud_detection already dedupes internally (skips
    any transaction that already has an alert) -- so "a FraudAlert was just
    created by this call" is already exactly "genuinely new", no extra
    dedup tracking needed here. One batched notification per call that
    produces new alerts, never one per alert."""
    if refresh:
        created = run_fraud_detection(db, merchant.id)
        db.commit()
        if created and wants_fraud_notifications(merchant):
            subject = f"{len(created)} new fraud alert(s) detected"
            body = format_member_bullet_list(
                [
                    f"{a.member.first_name} {a.member.last_name}: {explain_fraud_finding(a.reason, a.details)}"
                    for a in created
                ]
            )
            notify_merchant(merchant, subject, body, background_tasks)

    alerts = (
        db.query(FraudAlert)
        .join(Member, FraudAlert.member_id == Member.id)
        .filter(Member.merchant_id == merchant.id)
        .order_by(FraudAlert.score.desc())
        .all()
    )
    return [
        FraudAlertOut(
            id=a.id,
            transaction_id=a.transaction_id,
            member_id=a.member_id,
            member_name=f"{a.member.first_name} {a.member.last_name}".strip() or "Unknown customer",
            reason=a.reason,
            explanation=explain_fraud_finding(a.reason, a.details),
            score=a.score,
            details=a.details,
            resolved=a.resolved,
            created_at=a.created_at,
        )
        for a in alerts
    ]
