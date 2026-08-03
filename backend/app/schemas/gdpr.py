"""GDPR technical-pass schemas (PLAN_BATCH3.md §1): member erasure result
and the combined subject-access/portability export.

`MemberExportOut` includes `winback_offers` and `experiment_assignments`:
both `WinbackOffer` (PLAN_BATCH3.md §4) and `ExperimentAssignment`
(PLAN_BATCH3.md §5) hold real, identifiable personal data about a specific
member -- which reward they were comped and at what churn score a win-back
offer fired, and which A/B-test cohort/variant a member was placed in --
so both must round-trip in a UK GDPR Art. 15/20 export, same as
transactions/redemptions/fraud_alerts below.
"""
from datetime import datetime

from pydantic import BaseModel

from app.schemas.ai import FraudAlertOut
from app.schemas.experiments import ExperimentAssignmentOut
from app.schemas.member import MemberOut
from app.schemas.reward import RedemptionOut
from app.schemas.transaction import TransactionOut
from app.schemas.winback import WinbackOfferOut


class MemberErasureResult(BaseModel):
    member_id: str
    erased_at: datetime
    already_erased: bool


class MemberExportOut(BaseModel):
    """One combined machine-readable export covering both UK GDPR Art. 15
    (subject access) and Art. 20 (portability) -- see PLAN_BATCH3.md §1b
    for the explicit assumption flagged there (a solicitor should confirm
    this combined framing, not something asserted as definitively
    compliant)."""

    member: MemberOut
    transactions: list[TransactionOut]
    redemptions: list[RedemptionOut]
    fraud_alerts: list[FraudAlertOut]
    winback_offers: list[WinbackOfferOut]
    experiment_assignments: list[ExperimentAssignmentOut]
    exported_at: datetime
