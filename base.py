"""Member CRUD + list (scoped to the authenticated merchant)."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.ai.churn_model import score_member_churn
from app.api.deps import get_current_merchant
from app.db.base import get_db
from app.db.models import Member, Merchant
from app.schemas.member import MemberCreate, MemberOut, MemberWithChurn

router = APIRouter(prefix="/api/v1/members", tags=["members"])


@router.get("", response_model=list[MemberWithChurn])
def list_members(
    include_churn: bool = True,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(get_current_merchant),
) -> list[MemberWithChurn]:
    members = db.query(Member).filter(Member.merchant_id == merchant.id).all()
    results: list[MemberWithChurn] = []
    for m in members:
        out = MemberWithChurn.model_validate(m)
        if include_churn:
            churn = score_member_churn(db, m)
            out.churn_risk_score = churn.churn_risk_score
            out.churn_risk_band = churn.risk_band
        results.append(out)
    return results


@router.get("/{member_id}", response_model=MemberWithChurn)
def get_member(
    member_id: str,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(get_current_merchant),
) -> MemberWithChurn:
    member = db.query(Member).filter(Member.id == member_id, Member.merchant_id == merchant.id).first()
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    out = MemberWithChurn.model_validate(member)
    churn = score_member_churn(db, member)
    out.churn_risk_score = churn.churn_risk_score
    out.churn_risk_band = churn.risk_band
    return out


@router.post("", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
def create_member(
    payload: MemberCreate,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(get_current_merchant),
) -> Member:
    member = Member(
        merchant_id=merchant.id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=payload.email,
        tier=payload.tier,
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return member
