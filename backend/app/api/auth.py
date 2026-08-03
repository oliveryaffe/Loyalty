"""Merchant admin auth: signup (for demo/dev convenience) + JWT login.

Signup creates a Merchant (pure business entity) plus its first TeamMember
(role=admin) in the same transaction. Login authenticates a TeamMember by
email; the JWT `sub` claim is the TeamMember id, with `merchant_id` and
`role` carried as extra claims.

This endpoint is deliberately NOT gated by `require_active_subscription`
(PLAN_BATCH3.md §2) -- see app/api/deps.py's exemption list: a merchant
must be able to log in to *reach* the lock screen / billing in the first
place.

Judgment call (flagged inline, not in the original plan text): a freshly
signed-up Merchant here starts with `subscription_status="trialing"` and
`trial_ends_at` TRIAL_PERIOD_DAYS (14) out, rather than `None`. This
endpoint is Ledgerly's own direct account-creation path (no Stripe
involved at all) -- if new signups started life hard-locked
(`subscription_status=None` is a hard-lock value under
`require_active_subscription`), nobody could ever use the product long
enough to decide to subscribe via Stripe Checkout. Mirrors the same
14-day/card-required-upfront trial the plan specifies for the Stripe
Checkout Session path (`app/services/billing.py::TRIAL_PERIOD_DAYS`) --
this is the "you signed up directly, here's your trial" equivalent for
merchants who haven't been through Checkout yet.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.base import get_db
from app.db.models import Merchant, TeamMember, TeamRole
from app.schemas.auth import MeOut, MerchantLogin, MerchantSignup, Token
from app.services.billing import TRIAL_PERIOD_DAYS
from app.services.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/signup", response_model=MeOut, status_code=status.HTTP_201_CREATED)
def signup(payload: MerchantSignup, db: Session = Depends(get_db)) -> MeOut:
    existing = db.query(TeamMember).filter(TeamMember.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    trial_ends_at = datetime.now(timezone.utc) + timedelta(days=TRIAL_PERIOD_DAYS)
    merchant = Merchant(
        business_name=payload.business_name,
        subscription_status="trialing",
        trial_ends_at=trial_ends_at,
    )
    db.add(merchant)
    db.flush()

    team_member = TeamMember(
        merchant_id=merchant.id,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=TeamRole.ADMIN.value,
    )
    db.add(team_member)
    db.commit()
    db.refresh(team_member)
    db.refresh(merchant)

    return MeOut(
        id=team_member.id,
        merchant_id=merchant.id,
        business_name=merchant.business_name,
        email=team_member.email,
        role=team_member.role,
    )


@router.post("/login", response_model=Token)
def login(payload: MerchantLogin, db: Session = Depends(get_db)) -> Token:
    team_member = db.query(TeamMember).filter(TeamMember.email == payload.email).first()
    if (
        team_member is None
        or not team_member.is_active
        or not verify_password(payload.password, team_member.hashed_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    token = create_access_token(
        subject=team_member.id,
        extra_claims={"merchant_id": team_member.merchant_id, "role": team_member.role},
    )
    return Token(access_token=token)


@router.get("/me", response_model=MeOut)
def me(current_user: TeamMember = Depends(get_current_user)) -> MeOut:
    return MeOut(
        id=current_user.id,
        merchant_id=current_user.merchant_id,
        business_name=current_user.merchant.business_name,
        email=current_user.email,
        role=current_user.role,
    )
