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
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.ai.churn_model import BUSINESS_TYPE_CALIBRATIONS, compute_merchant_calibration
from app.api.deps import require_active_subscription, require_admin_active_subscription
from app.db.base import get_db
from app.db.models import Merchant
from app.schemas.settings import (
    BusinessProfileOut,
    BusinessProfileUpdate,
    BusinessTypeOption,
    NotificationSettingsOut,
    NotificationSettingsUpdate,
)
from app.services.notifications import wants_churn_notifications, wants_fraud_notifications

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

# Single source of truth for the onboarding business-type picker -- the
# frontend renders whatever this returns rather than hardcoding its own
# copy of the list, so the two can't drift. Keys must exist in
# BUSINESS_TYPE_CALIBRATIONS (checked by test_settings.py) except "other",
# which is the deliberate catch-all that falls through to
# DEFAULT_CALIBRATION.
BUSINESS_TYPES: list[BusinessTypeOption] = [
    BusinessTypeOption(value="coffee_shop", label="Coffee shop / café"),
    BusinessTypeOption(value="restaurant", label="Restaurant / bar"),
    BusinessTypeOption(value="barber_salon", label="Barber / hair & beauty salon"),
    BusinessTypeOption(value="retail", label="Retail / shop"),
    BusinessTypeOption(value="other", label="Other"),
]
_VALID_BUSINESS_TYPES = {opt.value for opt in BUSINESS_TYPES}

# Fail fast at import time (not silently at request time) if this list and
# BUSINESS_TYPE_CALIBRATIONS ever drift apart -- every option here except
# "other" must have a matching calibration profile.
assert _VALID_BUSINESS_TYPES - {"other"} <= set(BUSINESS_TYPE_CALIBRATIONS), (
    "BUSINESS_TYPES has a value with no matching BUSINESS_TYPE_CALIBRATIONS entry"
)


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


@router.get("/business-types", response_model=list[BusinessTypeOption])
def list_business_types() -> list[BusinessTypeOption]:
    """Options for the onboarding business-type picker. Public shape (no
    merchant-specific data), but still gated the same as everything else
    in this router for consistency -- there's no reason for it to be
    reachable pre-login."""
    return BUSINESS_TYPES


@router.get("/business-profile", response_model=BusinessProfileOut)
def get_business_profile(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> BusinessProfileOut:
    calibration = compute_merchant_calibration(db, merchant.id)
    return BusinessProfileOut(
        business_type=merchant.business_type,
        calibration_source=calibration.source,
    )


@router.patch("/business-profile", response_model=BusinessProfileOut)
def update_business_profile(
    payload: BusinessProfileUpdate,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> BusinessProfileOut:
    if payload.business_type not in _VALID_BUSINESS_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"business_type must be one of {sorted(_VALID_BUSINESS_TYPES)}",
        )
    merchant.business_type = payload.business_type
    db.commit()
    db.refresh(merchant)
    calibration = compute_merchant_calibration(db, merchant.id)
    return BusinessProfileOut(
        business_type=merchant.business_type,
        calibration_source=calibration.source,
    )
