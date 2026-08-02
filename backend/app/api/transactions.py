"""Transaction ingestion (earn events) -- stands in for a real POS/e-commerce
webhook per PLAN.md assumption A4."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_merchant
from app.db.base import get_db
from app.db.models import Member, Merchant, Transaction
from app.schemas.transaction import TransactionCreate, TransactionOut
from app.services.ledger import InactiveMemberError, earn_points

router = APIRouter(prefix="/api/v1/transactions", tags=["transactions"])


@router.post("", response_model=TransactionOut, status_code=status.HTTP_201_CREATED)
def ingest_transaction(
    payload: TransactionCreate,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(get_current_merchant),
) -> Transaction:
    member = (
        db.query(Member)
        .filter(Member.id == payload.member_id, Member.merchant_id == merchant.id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")

    try:
        txn = earn_points(db, member, payload.amount_usd, channel=payload.channel)
    except InactiveMemberError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    db.refresh(txn)
    return txn


@router.get("", response_model=list[TransactionOut])
def list_transactions(
    member_id: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(get_current_merchant),
) -> list[Transaction]:
    q = (
        db.query(Transaction)
        .join(Member, Transaction.member_id == Member.id)
        .filter(Member.merchant_id == merchant.id)
    )
    if member_id:
        q = q.filter(Transaction.member_id == member_id)
    return q.order_by(Transaction.created_at.desc()).limit(limit).all()
