"""Multi-location roll-up schemas -- see app/services/locations.py."""
from datetime import datetime

from pydantic import BaseModel


class LocationOut(BaseModel):
    id: str
    name: str
    created_at: datetime

    class Config:
        from_attributes = True


class LocationCreate(BaseModel):
    name: str


class LocationRollupOut(BaseModel):
    location_id: str | None
    name: str
    member_count: int
    high_risk_count: int
    predicted_value_90d: float


class MemberLocationUpdate(BaseModel):
    location_id: str | None
