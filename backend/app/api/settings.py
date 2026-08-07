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
    CustomerDataSourceOption,
    CustomerDataSourceUpdate,
    NotificationSettingsOut,
    NotificationSettingsUpdate,
)
from app.services.digest import wants_weekly_digest
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

# Second onboarding question (see app/db/models.py::Merchant
# .customer_data_source): how does this merchant currently identify
# individual repeat customers, if at all. Purely informational -- doesn't
# gate signup or any feature -- but "none" carries a hint pointing at the
# fastest realistic fix (turning on the till's own loyalty feature),
# since a merchant with genuinely zero customer-identifying data will
# otherwise sign up, see an empty dashboard, and have no idea why.
CUSTOMER_DATA_SOURCES: list[CustomerDataSourceOption] = [
    CustomerDataSourceOption(
        value="loyalty_app",
        label="A loyalty program (Square Loyalty, Loyverse, a punch-card app, etc.)",
    ),
    CustomerDataSourceOption(
        value="booking_app",
        label="A booking app (Fresha, Squire, Treatwell, etc.)",
    ),
    CustomerDataSourceOption(
        value="checkout_or_online",
        label="We ask for email at checkout, on receipts, or through online ordering",
    ),
    CustomerDataSourceOption(
        value="esp_list",
        label="We already have a Mailchimp/Klaviyo list from somewhere else",
    ),
    CustomerDataSourceOption(
        value="none",
        label="We don't currently collect this",
        hint=(
            "Ledgerly reads your existing customer data -- it can't create it from nothing. "
            "The fastest way to start is turning on your till's loyalty feature (most, including "
            "Square Loyalty, are free or near-free and take a few minutes to switch on) so "
            "customers leave an email at checkout. In the meantime, explore with sample data below."
        ),
    ),
]
_VALID_CUSTOMER_DATA_SOURCES = {opt.value for opt in CUSTOMER_DATA_SOURCES}


def _to_out(merchant: Merchant) -> NotificationSettingsOut:
    return NotificationSettingsOut(
        notification_slack_webhook_url=merchant.notification_slack_webhook_url,
        notification_email=merchant.notification_email,
        notify_on_churn_risk=wants_churn_notifications(merchant),
        notify_on_fraud_alert=wants_fraud_notifications(merchant),
        notify_weekly_digest=wants_weekly_digest(merchant),
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
    if "notify_weekly_digest" in data:
        merchant.notify_weekly_digest = data["notify_weekly_digest"]

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


@router.get("/data-sources", response_model=list[CustomerDataSourceOption])
def list_data_sources() -> list[CustomerDataSourceOption]:
    """Options for onboarding's second question -- how the merchant
    currently identifies repeat customers. Same "frontend renders
    whatever this returns" pattern as list_business_types above."""
    return CUSTOMER_DATA_SOURCES


def _business_profile_out(db: Session, merchant: Merchant) -> BusinessProfileOut:
    calibration = compute_merchant_calibration(db, merchant.id)
    return BusinessProfileOut(
        business_type=merchant.business_type,
        calibration_source=calibration.source,
        customer_data_source=merchant.customer_data_source,
    )


@router.get("/business-profile", response_model=BusinessProfileOut)
def get_business_profile(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> BusinessProfileOut:
    return _business_profile_out(db, merchant)


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
    return _business_profile_out(db, merchant)


@router.patch("/customer-data-source", response_model=BusinessProfileOut)
def update_customer_data_source(
    payload: CustomerDataSourceUpdate,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> BusinessProfileOut:
    """Onboarding's second question. Purely informational -- see
    app/db/models.py::Merchant.customer_data_source -- never gates
    signup, billing, or any feature; a merchant can answer "none" and
    keep using the product exactly as before (most likely with sample
    data, or by connecting a data source later)."""
    if payload.value not in _VALID_CUSTOMER_DATA_SOURCES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"value must be one of {sorted(_VALID_CUSTOMER_DATA_SOURCES)}",
        )
    merchant.customer_data_source = payload.value
    db.commit()
    db.refresh(merchant)
    return _business_profile_out(db, merchant)


@router.post("/business-profile/reset", response_model=BusinessProfileOut)
def reset_business_profile(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> BusinessProfileOut:
    """Clears business_type back to NULL so the onboarding picker
    (frontend's OnboardingModal, shown whenever business_type is NULL)
    replays on next dashboard load -- lets a merchant (or someone giving a
    demo) re-trigger the getting-started flow on an existing account
    instead of only ever seeing it once on a brand-new signup. Does not
    touch any transaction/member data -- purely resets the one onboarding
    flag, so real calibration (once a merchant has enough history) is
    unaffected either way. customer_data_source is deliberately left as
    it was -- that question isn't part of the "replay the picker" flow
    and a merchant's answer to it doesn't become stale just because
    they're re-choosing their business type."""
    merchant.business_type = None
    db.commit()
    db.refresh(merchant)
    return _business_profile_out(db, merchant)
