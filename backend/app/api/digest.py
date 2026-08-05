"""Weekly digest endpoints -- see app/services/digest.py for the underlying
computation and the "no scheduler, piggyback on GET /ai/churn" rationale.

GET /status and GET /preview are read-only (no gating beyond an active
subscription -- this is presentation of data the merchant can already see
elsewhere, not a new billable action). POST /send is the on-demand
equivalent of the automatic weekly send (e.g. a "send me a test now"
button in Settings) -- admin-gated, same reasoning as the notification
settings themselves, and records a "weekly_digest" UsageEvent since it's
real generated work same as a CSV upload or report download.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.api.deps import require_active_subscription, require_admin_active_subscription
from app.db.base import get_db
from app.db.models import Merchant
from app.schemas.digest import DigestSendResult, DigestStatusOut, WeeklyDigestOut
from app.services.digest import compute_weekly_digest, format_digest_email, wants_weekly_digest
from app.services.notifications import notify_merchant
from app.services.usage import record_usage_event

router = APIRouter(prefix="/api/v1/digest", tags=["digest"])


def _to_out(digest) -> WeeklyDigestOut:
    return WeeklyDigestOut(
        generated_at=digest.generated_at,
        total_members=digest.total_members,
        at_risk_count=digest.at_risk_count,
        at_risk_members=[
            {"member_id": m.member_id, "name": m.name, "recency_days": m.recency_days}
            for m in digest.at_risk_members
        ],
        predicted_value_90d=digest.predicted_value_90d,
        top_opportunity=digest.top_opportunity,
        headline=digest.headline,
    )


@router.get("/status", response_model=DigestStatusOut)
def get_digest_status(merchant: Merchant = Depends(require_active_subscription)) -> DigestStatusOut:
    return DigestStatusOut(
        enabled=wants_weekly_digest(merchant),
        last_digest_sent_at=merchant.last_digest_sent_at,
        has_notification_channel=bool(merchant.notification_slack_webhook_url or merchant.notification_email),
    )


@router.get("/preview", response_model=WeeklyDigestOut)
def preview_digest(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> WeeklyDigestOut:
    return _to_out(compute_weekly_digest(db, merchant))


@router.post("/send", response_model=DigestSendResult)
def send_digest(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> DigestSendResult:
    digest = compute_weekly_digest(db, merchant)
    subject, body = format_digest_email(digest, merchant.business_name)

    sent_via = []
    if merchant.notification_slack_webhook_url:
        sent_via.append("slack")
    if merchant.notification_email:
        sent_via.append("email")
    notify_merchant(merchant, subject, body, background_tasks)

    now = datetime.now(timezone.utc)
    merchant.last_digest_sent_at = now
    record_usage_event(db, merchant, "weekly_digest")
    db.commit()

    return DigestSendResult(sent_via=sent_via, last_digest_sent_at=now)
