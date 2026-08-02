from datetime import datetime

from pydantic import BaseModel, Field

from app.config import settings


class TransactionCreate(BaseModel):
    """Ingest an earn event (e.g. from a POS/e-commerce purchase webhook)."""

    member_id: str
    amount_usd: float = Field(
        gt=0,
        le=settings.max_transaction_amount_usd,
        description=(
            "Purchase amount in USD. Must be positive and no greater than "
            f"{settings.max_transaction_amount_usd:,.0f} (see "
            "Settings.max_transaction_amount_usd) to guard against a single "
            "call minting an unbounded number of points."
        ),
    )
    channel: str = "pos"


class TransactionOut(BaseModel):
    id: str
    member_id: str
    type: str
    amount_usd: float
    points: int
    channel: str
    created_at: datetime

    class Config:
        from_attributes = True
