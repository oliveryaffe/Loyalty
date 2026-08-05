"""Stripe billing (PLAN_BATCH3.md §2): Checkout/Portal session creation,
the current-subscription read, and the Stripe webhook.

Deliberately NOT behind `require_active_subscription` anywhere in this
router (it uses the plain `get_current_user`/`require_admin` dependencies
instead, or no auth at all for the webhook) -- a merchant that is hard-
locked must still be able to reach every endpoint here to resubscribe, and
the webhook is Stripe's own server calling us, not a merchant session. See
app/api/deps.py::require_active_subscription's docstring for the full
exemption list.
"""
from __future__ import annotations

import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.config import settings
from app.db.base import get_db
from app.db.models import BillingEvent, TeamMember
from app.schemas.billing import (
    CheckoutSessionOut,
    CheckoutSessionRequest,
    PlanOut,
    PortalSessionOut,
    SubscriptionOut,
    UsageOut,
)
from app.services.billing import (
    create_checkout_session,
    create_portal_session,
    handle_stripe_event,
    is_stripe_configured,
    price_id_for_tier,
    verify_and_parse_webhook,
)
from app.services.usage import PLAN_DEFINITIONS, compute_usage_summary

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

logger = logging.getLogger(__name__)

STRIPE_SIGNATURE_HEADER = "Stripe-Signature"


def _require_stripe_configured() -> None:
    """Billing endpoints must fail clearly (503) rather than raise a raw
    exception when the owner hasn't supplied Stripe credentials yet -- see
    PLAN_BATCH3.md §2's "External dependency" note. This app ships and runs
    fine with zero Stripe config (local dev, CI); only these endpoints are
    affected."""
    if not is_stripe_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not configured on this server yet.",
        )


@router.post("/checkout-session", response_model=CheckoutSessionOut)
def create_checkout_session_endpoint(
    payload: CheckoutSessionRequest,
    current_user: TeamMember = Depends(require_admin),
) -> CheckoutSessionOut:
    _require_stripe_configured()
    if price_id_for_tier(payload.tier) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No Stripe Price ID configured for tier '{payload.tier}' yet.",
        )

    session = create_checkout_session(
        current_user.merchant, payload.tier, customer_email=current_user.email
    )
    return CheckoutSessionOut(checkout_url=session.url)


@router.post("/portal-session", response_model=PortalSessionOut)
def create_portal_session_endpoint(
    current_user: TeamMember = Depends(require_admin),
) -> PortalSessionOut:
    _require_stripe_configured()
    merchant = current_user.merchant
    if not merchant.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This merchant has no billing account yet -- subscribe first.",
        )

    session = create_portal_session(merchant)
    return PortalSessionOut(portal_url=session.url)


@router.get("/subscription", response_model=SubscriptionOut)
def get_subscription(
    current_user: TeamMember = Depends(get_current_user),
) -> SubscriptionOut:
    merchant = current_user.merchant
    return SubscriptionOut(
        subscription_status=merchant.subscription_status,
        subscription_tier=merchant.subscription_tier,
        subscription_current_period_end=merchant.subscription_current_period_end,
        trial_ends_at=merchant.trial_ends_at,
    )


@router.get("/plans", response_model=list[PlanOut])
def list_plans() -> list[PlanOut]:
    """Usage-based pricing table (app/services/usage.py) -- single source
    of truth the frontend renders instead of hardcoding plan copy, same
    pattern as GET /settings/business-types. Not gated behind an active
    subscription (this router never is -- see module docstring): a
    locked-out merchant needs to see plan options to resubscribe."""
    return [
        PlanOut(
            tier=p.tier,
            name=p.name,
            base_price_gbp=p.base_price_gbp,
            included_runs=p.included_runs,
            overage_price_gbp=p.overage_price_gbp,
        )
        for p in PLAN_DEFINITIONS.values()
    ]


@router.get("/usage", response_model=UsageOut)
def get_usage(
    db: Session = Depends(get_db),
    current_user: TeamMember = Depends(get_current_user),
) -> UsageOut:
    """Current calendar-month insight-run usage vs. the merchant's plan
    allowance. Uses get_current_user (not require_active_subscription),
    same reasoning as GET /subscription -- a merchant deciding whether to
    resubscribe needs to see this too."""
    summary = compute_usage_summary(db, current_user.merchant)
    return UsageOut(
        period_start=summary.period_start,
        tier=summary.plan.tier,
        plan_name=summary.plan.name,
        included_runs=summary.plan.included_runs,
        insight_runs_used=summary.insight_runs_used,
        overage_runs=summary.overage_runs,
        estimated_overage_cost_gbp=summary.estimated_overage_cost_gbp,
    )


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Mirrors app/api/webhooks.py's Shopify handler exactly: read the raw
    body via `await request.body()` *before* any parsing (Stripe signs raw
    bytes), verify via `stripe.Webhook.construct_event`, 401 on a bad
    signature. Idempotency: insert a BillingEvent row keyed on
    stripe_event_id first and catch the resulting IntegrityError as
    "already processed, no-op" -- the same DB-level-UNIQUE-constraint
    pattern as Transaction.external_order_id's Shopify dedup, not a
    SELECT-then-branch check (see app/db/models.py::BillingEvent's
    docstring)."""
    if not settings.stripe_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing webhook is not configured on this server yet.",
        )

    raw_body = await request.body()
    signature_header = request.headers.get(STRIPE_SIGNATURE_HEADER)

    try:
        event = verify_and_parse_webhook(raw_body, signature_header)
    except stripe.SignatureVerificationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Malformed webhook payload") from exc

    billing_event = BillingEvent(
        stripe_event_id=event["id"],
        event_type=event["type"],
        raw_payload=raw_body.decode("utf-8", errors="replace"),
    )
    db.add(billing_event)
    try:
        db.flush()
    except IntegrityError:
        # Another delivery of the same event already won the race and
        # committed a BillingEvent with this stripe_event_id -- caught here
        # via the DB-level UNIQUE constraint, not a prior SELECT. Roll back
        # and report success (Stripe redelivers non-2xx responses, and this
        # event genuinely was already processed).
        db.rollback()
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "duplicate_ignored"})

    try:
        merchant = handle_stripe_event(db, event)
    except Exception:
        db.rollback()
        logger.exception("Unhandled error processing Stripe webhook event %s", event.get("id"))
        raise

    if merchant is not None:
        billing_event.merchant_id = merchant.id

    db.commit()
    return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "processed"})
