"""Stripe billing integration (PLAN_BATCH3.md §2): tier <-> Price ID
mapping, Checkout/Billing-Portal session creation, and webhook event
handling.

Kept as pure-ish functions (mirrors app/services/shopify.py's separation of
concerns) so app/api/billing.py's router stays thin and the Stripe-specific
logic is testable by mocking the `stripe` module's call sites directly
(unittest.mock/monkeypatch), never hitting Stripe's real network -- no real
Stripe credentials are available in this environment. Written against the
real `stripe` SDK's documented API shape (`stripe.checkout.Session`,
`stripe.billing_portal.Session`, `stripe.Webhook.construct_event`) so it is
correct once the owner supplies real test/live keys.

Webhook event objects are read via dict-style access (`event["type"]`,
`obj.get("customer")`) rather than attribute access throughout. Real Stripe
`StripeObject`/`Event` instances support both, but this keeps the handlers
usable with a plain Python dict too -- both in tests (mocking
`stripe.Webhook.construct_event` to return a plain dict is far simpler than
constructing a real StripeObject) and, more importantly, because Stripe's
own webhook payloads are just JSON -- dict access is the more literal,
implementation-independent way to read them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import stripe
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Merchant

logger = logging.getLogger(__name__)

# Assumption (flagged in PLAN_BATCH3.md §2): 14-day free trial, card
# required upfront -- trial_period_days on the Checkout Session's
# subscription_data. Also applied to app/api/auth.py's direct signup flow
# (see that module's docstring) so a merchant who signs up without ever
# touching Stripe checkout still gets a bounded trial window rather than
# being hard-locked (subscription_status=None) the instant they sign up.
TRIAL_PERIOD_DAYS = 14

# Maps a subscription tier name to the app/config.py Settings attribute
# holding its Stripe Price ID -- one indirection point so price_id_for_tier
# / tier_for_price_id stay in sync by construction.
_TIER_PRICE_SETTINGS_ATTR = {
    "starter": "stripe_price_id_starter",
    "growth": "stripe_price_id_growth",
    "scale": "stripe_price_id_scale",
}


def is_stripe_configured() -> bool:
    """True once the owner has supplied a secret key. Checkout/portal
    session creation needs at least this; the webhook path additionally
    needs `stripe_webhook_secret` (checked separately, see
    app/api/billing.py)."""
    return bool(settings.stripe_secret_key)


def price_id_for_tier(tier: str) -> str | None:
    attr = _TIER_PRICE_SETTINGS_ATTR.get(tier)
    if attr is None:
        return None
    return getattr(settings, attr)


def tier_for_price_id(price_id: str | None) -> str | None:
    if not price_id:
        return None
    for tier, attr in _TIER_PRICE_SETTINGS_ATTR.items():
        configured = getattr(settings, attr)
        if configured and configured == price_id:
            return tier
    return None


def _stripe_api_key() -> None:
    stripe.api_key = settings.stripe_secret_key


def create_checkout_session(merchant: Merchant, tier: str, customer_email: str) -> Any:
    """Creates a Stripe Checkout Session (mode=subscription) for the given
    tier. Returns the Stripe Session object (`.url` is what the caller
    needs) or None if `tier` doesn't map to a configured Price ID.

    `client_reference_id=merchant.id` is how the webhook handler below
    correlates the eventual `checkout.session.completed` event back to this
    merchant (see `_resolve_merchant`) before `stripe_customer_id` is known.
    """
    price_id = price_id_for_tier(tier)
    if price_id is None:
        return None

    _stripe_api_key()
    identity_kwargs: dict[str, str] = (
        {"customer": merchant.stripe_customer_id}
        if merchant.stripe_customer_id
        else {"customer_email": customer_email}
    )
    return stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        client_reference_id=merchant.id,
        subscription_data={"trial_period_days": TRIAL_PERIOD_DAYS},
        success_url=settings.billing_success_url,
        cancel_url=settings.billing_cancel_url,
        **identity_kwargs,
    )


def create_portal_session(merchant: Merchant) -> Any:
    """Creates a Stripe Billing Portal session for an already-subscribed
    merchant. Caller (app/api/billing.py) is responsible for the 404 when
    `merchant.stripe_customer_id` is None -- this function assumes it's
    already set."""
    _stripe_api_key()
    return stripe.billing_portal.Session.create(
        customer=merchant.stripe_customer_id,
        return_url=settings.billing_success_url,
    )


def verify_and_parse_webhook(raw_body: bytes, signature_header: str | None) -> Any:
    """Verifies the Stripe webhook signature over the *raw* request body and
    returns the parsed event. Raises stripe.SignatureVerificationError (the
    caller maps this to 401, same pattern as
    app/services/shopify.py::verify_shopify_hmac / app/api/webhooks.py) or
    ValueError for a malformed payload."""
    return stripe.Webhook.construct_event(raw_body, signature_header, settings.stripe_webhook_secret)


def _resolve_merchant(db: Session, obj: dict) -> Merchant | None:
    """Finds the Merchant a webhook event's object (checkout Session,
    Subscription, or Invoice) belongs to. Tries, in order: Stripe customer
    id (the common case once a merchant has subscribed once), Stripe
    subscription id (fallback, e.g. an out-of-order delivery before
    stripe_customer_id was persisted), then `client_reference_id` (the
    *first* checkout.session.completed for a merchant -- neither
    stripe_customer_id nor stripe_subscription_id is on file yet)."""
    customer_id = obj.get("customer")
    if customer_id:
        merchant = db.query(Merchant).filter(Merchant.stripe_customer_id == customer_id).first()
        if merchant is not None:
            return merchant

    subscription_id = obj.get("id") if obj.get("object") == "subscription" else obj.get("subscription")
    if subscription_id:
        merchant = db.query(Merchant).filter(Merchant.stripe_subscription_id == subscription_id).first()
        if merchant is not None:
            return merchant

    client_reference_id = obj.get("client_reference_id")
    if client_reference_id:
        return db.get(Merchant, client_reference_id)

    return None


def _to_datetime(unix_ts: int | None) -> datetime | None:
    if unix_ts is None:
        return None
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc)


def _handle_checkout_completed(db: Session, obj: dict) -> Merchant | None:
    merchant = _resolve_merchant(db, obj)
    if merchant is None:
        logger.warning(
            "Stripe checkout.session.completed: no merchant resolved for session %s", obj.get("id")
        )
        return None
    if obj.get("customer"):
        merchant.stripe_customer_id = obj["customer"]
    if obj.get("subscription"):
        merchant.stripe_subscription_id = obj["subscription"]
    return merchant


def _handle_subscription_upsert(db: Session, obj: dict) -> Merchant | None:
    """Handles both customer.subscription.created and .updated -- same
    shape, same fields of interest (status, current_period_end, price)."""
    merchant = _resolve_merchant(db, obj)
    if merchant is None:
        logger.warning("Stripe subscription event: no merchant resolved for subscription %s", obj.get("id"))
        return None

    if obj.get("id"):
        merchant.stripe_subscription_id = obj["id"]
    if obj.get("customer"):
        merchant.stripe_customer_id = obj["customer"]

    status_value = obj.get("status")
    if status_value:
        merchant.subscription_status = status_value

    period_end = _to_datetime(obj.get("current_period_end"))
    if period_end is not None:
        merchant.subscription_current_period_end = period_end

    items = ((obj.get("items") or {}).get("data")) or []
    if items:
        price = items[0].get("price") or {}
        tier = tier_for_price_id(price.get("id"))
        if tier is not None:
            merchant.subscription_tier = tier

    return merchant


def _handle_subscription_deleted(db: Session, obj: dict) -> Merchant | None:
    merchant = _resolve_merchant(db, obj)
    if merchant is None:
        return None
    merchant.subscription_status = "canceled"
    return merchant


def _handle_invoice_payment_failed(db: Session, obj: dict) -> Merchant | None:
    """Soft lock: PLAN_BATCH3.md §2's `past_due` state. Stripe's own
    dunning schedule keeps retrying the card; the merchant stays fully
    functional (require_active_subscription treats past_due as allowed),
    the frontend just shows a warning banner."""
    merchant = _resolve_merchant(db, obj)
    if merchant is None:
        return None
    merchant.subscription_status = "past_due"
    return merchant


def _handle_invoice_paid(db: Session, obj: dict) -> Merchant | None:
    """Recovery from `past_due` (or straightforward renewal payment)."""
    merchant = _resolve_merchant(db, obj)
    if merchant is None:
        return None
    merchant.subscription_status = "active"
    return merchant


_EVENT_HANDLERS = {
    "checkout.session.completed": _handle_checkout_completed,
    "customer.subscription.created": _handle_subscription_upsert,
    "customer.subscription.updated": _handle_subscription_upsert,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.payment_failed": _handle_invoice_payment_failed,
    "invoice.paid": _handle_invoice_paid,
}


def handle_stripe_event(db: Session, event: dict) -> Merchant | None:
    """Dispatches a verified Stripe event to the matching handler and
    returns the affected Merchant (if resolved), so the caller can stamp
    `BillingEvent.merchant_id`. Unrecognized event types are a deliberate
    no-op -- Stripe sends many event types this app doesn't act on, and the
    webhook endpoint must still 200 them (an unhandled type is not an
    error), not raise."""
    event_type = event.get("type")
    handler = _EVENT_HANDLERS.get(event_type)
    if handler is None:
        return None
    obj = ((event.get("data") or {}).get("object")) or {}
    return handler(db, obj)
