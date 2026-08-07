"""Win-back worklist schemas -- reworked into a read-only suggestion list.
See app/services/winback.py for why: no more rule "run", no more offer
history, no more auto_trigger. `WinbackRule` now just stores a reward
preference + threshold for the worklist to use when suggesting what to
offer; it doesn't gate any automatic action anymore."""
from datetime import datetime

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


class WinbackEmailOut(BaseModel):
    """Result of a single, merchant-triggered win-back email send -- see
    app/services/winback.py::send_winback_email. `reason` is one of
    "sent" | "cooldown" | "smtp_not_configured" | "send_failed", surfaced
    directly to the merchant rather than collapsed into a generic
    success/failure flag, since each case needs different UI copy (a
    cooldown isn't an error, "not configured" isn't the merchant's fault
    to fix)."""

    sent: bool
    reason: str
    cooldown_until: datetime | None = None
