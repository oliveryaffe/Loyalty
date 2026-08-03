"""Per-merchant notification configuration (PLAN_BATCH3.md §3): Slack
webhook URL, notification email, and on/off toggles for churn-escalation
and fraud-alert notifications. Self-serve, no owner dependency -- each
merchant supplies their own Slack "Incoming Webhooks" URL directly here.

Gated with `require_active_subscription`/`require_admin_active_subscription`
(not the older `get_current_merchant`), consistent with every other
paid-tier feature router in this batch -- notifications are a Growth-tier-
and-up feature per the pricing table, with no exemption reason like
billing/auth/webhooks/GDPR have.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import require_active_subscription, require_admin_active_subscription
from app.db.base import get_db
from app.db.models import Merchant
from app.schemas.settings import NotificationSettingsOut, NotificationSettingsUpdate
from app.services.notifications import wants_churn_notifications, wants_fraud_notifications

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


def _to_out(merchant: Merchant) -> NotificationSettingsOut:
    return NotificationSettingsOut(
        notification_slack_webhook_url=merchant.notification_slack_webhook_url,
        notification_email=merchant.notification_email,
        notify_on_churn_risk=wants_churn_notifications(merchant),
        notify_on_fraud_alert=wants_fraud_notifications(merchant),
    )


@router.get("/notifications", response_model=NotificationSettingsOut)
def get_notification_settings(
    merchant: Merchant = Depends(require_active_subscription),
) -> NotificationSettingsOut:
    return _to_out(merchant)


@router.patch("/notifications", response_model=NotificationSettingsOut)
def update_notification_settings(
    payload: NotificationSettingsUpdate,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> NotificationSettingsOut:
    data = payload.model_dump(exclude_unset=True)

    if "notification_slack_webhook_url" in data:
        url = data["notification_slack_webhook_url"]
        merchant.notification_slack_webhook_url = str(url) if url is not None else None
    if "notification_email" in data:
        merchant.notification_email = data["notification_email"]
    if "notify_on_churn_risk" in data:
        merchant.notify_on_churn_risk = data["notify_on_churn_risk"]
    if "notify_on_fraud_alert" in data:
        merchant.notify_on_fraud_alert = data["notify_on_fraud_alert"]

    db.commit()
    db.refresh(merchant)
    return _to_out(merchant)
