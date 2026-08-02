from datetime import datetime

from pydantic import BaseModel, EmailStr


class MemberCreate(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    tier: str = "bronze"


class MemberOut(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: EmailStr
    points_balance: int
    tier: str
    is_active: bool
    joined_at: datetime
    last_activity_at: datetime

    class Config:
        from_attributes = True


class MemberWithChurn(MemberOut):
    churn_risk_score: float | None = None
    churn_risk_band: str | None = None
