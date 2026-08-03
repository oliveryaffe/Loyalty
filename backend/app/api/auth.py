"""Merchant admin auth: signup (for demo/dev convenience) + JWT login.

Signup creates a Merchant (pure business entity) plus its first TeamMember
(role=admin) in the same transaction. Login authenticates a TeamMember by
email; the JWT `sub` claim is the TeamMember id, with `merchant_id` and
`role` carried as extra claims.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.base import get_db
from app.db.models import Merchant, TeamMember, TeamRole
from app.schemas.auth import MeOut, MerchantLogin, MerchantSignup, Token
from app.services.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/signup", response_model=MeOut, status_code=status.HTTP_201_CREATED)
def signup(payload: MerchantSignup, db: Session = Depends(get_db)) -> MeOut:
    existing = db.query(TeamMember).filter(TeamMember.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    merchant = Merchant(business_name=payload.business_name)
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
