"""Merchant admin auth: signup (for demo/dev convenience) + JWT login."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_merchant
from app.db.base import get_db
from app.db.models import Merchant
from app.schemas.auth import MerchantLogin, MerchantOut, MerchantSignup, Token
from app.services.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/signup", response_model=MerchantOut, status_code=status.HTTP_201_CREATED)
def signup(payload: MerchantSignup, db: Session = Depends(get_db)) -> Merchant:
    existing = db.query(Merchant).filter(Merchant.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    merchant = Merchant(
        business_name=payload.business_name,
        email=payload.email,
        hashed_password=hash_password(payload.password),
    )
    db.add(merchant)
    db.commit()
    db.refresh(merchant)
    return merchant


@router.post("/login", response_model=Token)
def login(payload: MerchantLogin, db: Session = Depends(get_db)) -> Token:
    merchant = db.query(Merchant).filter(Merchant.email == payload.email).first()
    if merchant is None or not verify_password(payload.password, merchant.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    token = create_access_token(subject=merchant.id)
    return Token(access_token=token)


@router.get("/me", response_model=MerchantOut)
def me(current_merchant: Merchant = Depends(get_current_merchant)) -> Merchant:
    return current_merchant
