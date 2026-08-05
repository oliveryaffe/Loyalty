"""Weekly digest schemas -- see app/services/digest.py for the underlying
computation this wraps."""
from datetime import datetime

from pydantic import BaseModel


class DigestAtRiskMemberOut(BaseModel):
    member_id: str
    name: str
    recency_days: float


class WeeklyDigestOut(BaseModel):
    generated_at: datetime
    total_members: int
    at_risk_count: int
    at_risk_members: list[DigestAtRiskMemberOut]
    predicted_value_90d: float
    top_opportunity: str
    headline: str


class DigestSendResult(BaseModel):
    sent_via: list[str]
    last_digest_sent_at: datetime


class DigestStatusOut(BaseModel):
    enabled: bool
    last_digest_sent_at: datetime | None
    has_notification_channel: bool
