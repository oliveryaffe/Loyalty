"""Win-back campaign schemas (PLAN_BATCH3.md §4)."""
from datetime import datetime

from pydantic import BaseModel


class WinbackRuleIn(BaseModel):
    enabled: bool = False
    churn_risk_threshold: float = 65.0
    reward_id: str
    auto_trigger: bool = False


class WinbackRuleOut(BaseModel):
    id: str | None = None
    merchant_id: str
    enabled: bool
    churn_risk_threshold: float
    reward_id: str | None = None
    auto_trigger: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class WinbackRunResult(BaseModel):
    offers_sent: int
    member_ids: list[str]


class WinbackOfferOut(BaseModel):
    id: str
    merchant_id: str
    member_id: str
    rule_id: str
    redemption_id: str
    churn_risk_score_at_trigger: float
    triggered_by: str
    created_at: datetime

    class Config:
        from_attributes = True
