"""Notification settings schemas (PLAN_BATCH3.md §3)."""
from pydantic import BaseModel, EmailStr, HttpUrl


class NotificationSettingsOut(BaseModel):
    """`notify_on_churn_risk`/`notify_on_fraud_alert` are surfaced here as
    plain (non-Optional) bools -- the *effective* value (via
    app/services/notifications.py::wants_churn_notifications /
    wants_fraud_notifications, which treat NULL the same as True), not the
    raw nullable DB column. A settings-page toggle should reflect what will
    actually happen, not the DB's tri-state representation of it."""

    notification_slack_webhook_url: str | None
    notification_email: str | None
    notify_on_churn_risk: bool
    notify_on_fraud_alert: bool
    notify_weekly_digest: bool


class NotificationSettingsUpdate(BaseModel):
    """Partial update -- only fields explicitly present in the request body
    are applied (see app/api/settings.py, which reads this via
    `model_dump(exclude_unset=True)`), so a merchant can e.g. flip one
    toggle without having to resend the Slack URL/email too. Slack webhook
    URL is validated as a well-formed https:// URL (Pydantic HttpUrl) but
    not verified against Slack's own URL shape -- kept loose deliberately
    in case a merchant uses a compatible relay/proxy; the first real send
    attempt is the actual verification (failures logged, not a hard error
    on save)."""

    notification_slack_webhook_url: HttpUrl | None = None
    notification_email: EmailStr | None = None
    notify_on_churn_risk: bool | None = None
    notify_on_fraud_alert: bool | None = None
    notify_weekly_digest: bool | None = None


class BusinessTypeOption(BaseModel):
    value: str
    label: str


class BusinessProfileOut(BaseModel):
    """`business_type=None` means the merchant hasn't completed onboarding
    yet -- the frontend shows a one-question business-type picker on the
    dashboard whenever this is null, and stops once it's set (including to
    "other", a valid explicit answer). See app.ai.churn_model for how this
    feeds churn/future-value's calibration fallback.

    `calibration_source` mirrors MerchantCalibration.source from
    app.ai.churn_model -- "calibrated" once the merchant has enough of its
    own repeat-visit history (>= MIN_MEMBERS_WITH_REPEAT_VISITS), else
    "default_vertical" (using business_type's starting defaults) or
    "default" (generic starting defaults, no business_type set). Exposed
    so the Settings page can show which mode is actually active instead of
    leaving the business-type picker looking like a no-op once real
    calibration has taken over."""

    business_type: str | None
    calibration_source: str


class BusinessProfileUpdate(BaseModel):
    business_type: str
