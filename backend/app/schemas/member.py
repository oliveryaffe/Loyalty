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
    # Plain `str`, not `EmailStr`, deliberately: GDPR erasure
    # (POST /members/{id}/gdpr-erase, app/api/members.py) overwrites this
    # field with a non-resolvable placeholder address on the
    # `deleted.ledgerly.invalid` domain, and `.invalid` is an IANA
    # special-use TLD that `EmailStr`'s underlying `email-validator`
    # rejects outright at the syntax-check level (independent of any
    # deliverability check) -- an output-serialization schema shouldn't
    # re-validate a value the DB already holds. `MemberCreate.email` below
    # stays `EmailStr` so real member-creation input is still validated.
    email: str
    points_balance: int
    tier: str
    is_active: bool
    joined_at: datetime
    last_activity_at: datetime
    erased_at: datetime | None = None

    class Config:
        from_attributes = True


class MemberWithChurn(MemberOut):
    churn_risk_score: float | None = None
    churn_risk_band: str | None = None
    # Plain-English "why" (competitive-brief backlog item #5) -- see
    # app/ai/churn_model.py::explain_churn_risk. None whenever churn
    # scoring wasn't requested (include_churn=False) or hasn't run yet.
    churn_risk_explanation: str | None = None
