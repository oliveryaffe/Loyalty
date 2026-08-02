"""Reward catalog CRUD + redemption workflow."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_merchant
from app.db.base import get_db
from app.db.models import Member, Merchant, Redemption, RewardCatalogItem
from app.schemas.reward import RedemptionOut, RedemptionRequest, RewardCreate, RewardOut
from app.services.ledger import (
    InactiveMemberError,
    InsufficientBalanceError,
    RewardUnavailableError,
    TierIneligibleError,
    redeem_reward,
)

router = APIRouter(prefix="/api/v1/rewards", tags=["rewards"])


@router.get("", response_model=list[RewardOut])
def list_rewards(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(get_current_merchant),
) -> list[RewardCatalogItem]:
    return (
        db.query(RewardCatalogItem)
        .filter(RewardCatalogItem.merchant_id == merchant.id)
        .all()
    )


@router.post("", response_model=RewardOut, status_code=status.HTTP_201_CREATED)
def create_reward(
    payload: RewardCreate,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(get_current_merchant),
) -> RewardCatalogItem:
    reward = RewardCatalogItem(merchant_id=merchant.id, **payload.model_dump())
    db.add(reward)
    db.commit()
    db.refresh(reward)
    return reward


@router.post("/redeem", response_model=RedemptionOut)
def redeem(
    payload: RedemptionRequest,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(get_current_merchant),
) -> Redemption:
    member = (
        db.query(Member)
        .filter(Member.id == payload.member_id, Member.merchant_id == merchant.id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")

    reward = (
        db.query(RewardCatalogItem)
        .filter(
            RewardCatalogItem.id == payload.reward_id,
            RewardCatalogItem.merchant_id == merchant.id,
        )
        .first()
    )
    if reward is None:
        raise HTTPException(status_code=404, detail="Reward not found")

    try:
        redemption, _txn = redeem_reward(db, member, reward)
    except InsufficientBalanceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TierIneligibleError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (InactiveMemberError, RewardUnavailableError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    db.refresh(redemption)
    return redemption
