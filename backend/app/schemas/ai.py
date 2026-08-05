from datetime import datetime

from pydantic import BaseModel


class RecommendationOut(BaseModel):
    reward_id: str
    reward_name: str
    points_cost: int
    score: float
    reason: str


class ChurnScoreOut(BaseModel):
    member_id: str
    first_name: str
    last_name: str
    recency_days: float
    frequency: int
    monetary: float
    churn_risk_score: float
    risk_band: str
    # Plain-English "why" (competitive-brief backlog item #5) -- see
    # app/ai/churn_model.py::explain_churn_risk.
    explanation: str


class FraudAlertOut(BaseModel):
    id: str
    transaction_id: str
    member_id: str
    reason: str
    score: float
    details: str
    resolved: bool
    created_at: datetime

    class Config:
        from_attributes = True
