"""Win-back worklist schemas -- reworked into a read-only suggestion list.
See app/services/winback.py for why: no more rule "run", no more offer
history, no more auto_trigger. `WinbackRule` now just stores a reward
preference + threshold for the worklist to use when suggesting what to
offer; it doesn't gate any automatic action anymore."""
from pydantic import BaseModel


class WinbackRuleIn(BaseModel):
    enabled: bool = False
    churn_risk_threshold: float = 65.0
    reward_id: str


class WinbackRuleOut(BaseModel):
    id: str | None = None
    merchant_id: str
    enabled: bool
    churn_risk_threshold: float
    reward_id: str | None = None

    class Config:
        from_attributes = True


class WinbackWorklistEntryOut(BaseModel):
    member_id: str
    first_name: str
    last_name: str
    churn_risk_score: float
    risk_band: str
    suggested_reward_id: str | None = None
    suggested_reward_name: str | None = None
