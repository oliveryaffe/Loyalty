"""Shared FastAPI dependencies: DB session + JWT-authenticated user/merchant.

`get_current_user` decodes the JWT and loads the authenticated `TeamMember`.
`get_current_merchant` is kept as a thin wrapper around it (returns
`current_user.merchant`) so existing routers (members.py, transactions.py,
rewards.py, ai.py) that depend on `get_current_merchant` need zero changes
for the multi-user-accounts feature.

`require_active_subscription` (PLAN_BATCH3.md §2) is a second thin wrapper,
used in place of `get_current_merchant` on most routers, that additionally
hard-locks (402 Payment Required) a merchant whose subscription has lapsed.
See the module-level docstring on `ALLOWED_SUBSCRIPTION_STATUSES` below for
exactly which statuses are let through.
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import Merchant, TeamMember
from app.services.security import decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> TeamMember:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if token is None:
        raise credentials_exception

    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception

    team_member_id = payload.get("sub")
    if team_member_id is None:
        raise credentials_exception

    user = db.get(TeamMember, team_member_id)
    if user is None or not user.is_active:
        raise credentials_exception

    return user


def get_current_merchant(current_user: TeamMember = Depends(get_current_user)) -> Merchant:
    return current_user.merchant


def require_admin(current_user: TeamMember = Depends(get_current_user)) -> TeamMember:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return current_user


# Statuses that keep the dashboard/API fully usable (PLAN_BATCH3.md §2's
# gating design). `trialing`/`active` are the normal paying-or-in-trial
# states. `past_due` is deliberately included here too -- it is a SOFT
# lock only: Stripe is still auto-retrying the card on its own dunning
# schedule, and hard-blocking would break the merchant's own live retail
# business (their POS/Shopify store still wants to award points to real
# shoppers) over a card that might self-heal in days. The frontend polls
# `GET /billing/subscription` and shows a dismissible warning banner for
# `past_due` -- that's the only UX difference for this status, not an API
# difference. Every other value -- `canceled`, `unpaid`, `incomplete`,
# `incomplete_expired`, or `None` (no subscription ever started) -- is a
# HARD lock: `require_active_subscription` below raises 402.
ALLOWED_SUBSCRIPTION_STATUSES = {"trialing", "active", "past_due"}


def require_active_subscription(current_user: TeamMember = Depends(get_current_user)) -> Merchant:
    """Drop-in replacement for `get_current_merchant` used on most routers
    (PLAN_BATCH3.md §2) -- same return shape (`Merchant`), plus a
    `402 Payment Required` if the merchant is hard-locked (see
    `ALLOWED_SUBSCRIPTION_STATUSES` above for exactly which statuses pass).

    Deliberately NOT applied to: `app/api/auth.py` (must still be able to
    log in to reach the lock screen / billing), `app/api/billing.py` (must
    still be able to resubscribe), `app/api/webhooks.py` (Shopify order
    ingestion -- a third-party callback from the merchant's own live
    storefront; dropping real purchase events during a billing lapse would
    permanently lose the loyalty points a real shopper is owed), and the
    GDPR erasure/export endpoints in `app/api/members.py` (a compliance
    obligation doesn't pause because an invoice is unpaid). Those routers
    keep using `get_current_merchant`/`get_current_user`/`require_admin`
    unchanged.
    """
    merchant = current_user.merchant
    if merchant.subscription_status not in ALLOWED_SUBSCRIPTION_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                "An active subscription is required to use Ledgerly. "
                "Please subscribe or update your billing details."
            ),
        )
    return merchant


def require_admin_active_subscription(current_user: TeamMember = Depends(require_admin)) -> Merchant:
    """Composes `require_admin`'s role check with `require_active_subscription`'s
    hard-lock check, for admin-only write endpoints on paid-tier features
    (PLAN_BATCH3.md §3/§4 -- notifications settings, win-back rule/run).
    Unlike `app/api/billing.py` (which deliberately stays reachable for a
    hard-locked merchant so they can resubscribe) and the GDPR erasure/
    export endpoints (a compliance obligation that doesn't pause for an
    unpaid invoice), notifications and win-back are ordinary paid-tier
    product features (Growth tier and up per the pricing table) with no
    such exemption reason -- so they get both checks, matching the
    `require_active_subscription` convention used everywhere else in the
    app rather than the older, subscription-unaware `get_current_merchant`.
    """
    merchant = current_user.merchant
    if merchant.subscription_status not in ALLOWED_SUBSCRIPTION_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                "An active subscription is required to use Ledgerly. "
                "Please subscribe or update your billing details."
            ),
        )
    return merchant
