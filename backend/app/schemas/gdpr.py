"""GDPR technical-pass schemas (PLAN_BATCH3.md §1): member erasure result
and the combined subject-access/portability export.

`MemberExportOut` includes `experiment_assignments`: `ExperimentAssignment`
(PLAN_BATCH3.md §5) holds real, identifiable personal data about a specific
member -- which A/B-test cohort/variant they were placed in -- so it must
round-trip in a UK GDPR Art. 15/20 export, same as
transactions/redemptions/fraud_alerts below.

No `winback_offers` field: win-back was reworked into a read-only,
computed-on-demand worklist (see app/services/winback.py) that persists
nothing about a member, so there is no win-back personal data to export.
"""
from datetime import datetime

from pydantic import BaseModel

from app.schemas.ai import FraudAlertOut
from app.schemas.experiments import ExperimentAssignmentOut
from app.schemas.member import MemberOut
from app.schemas.reward import RedemptionOut
from app.schemas.transaction import TransactionOut


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
    experiment_assignments: list[ExperimentAssignmentOut]
    exported_at: datetime


class GdprAuditLogEntryOut(BaseModel):
    """One row of the Compliance tab's audit trail -- see
    app.db.models.GdprAuditLogEntry."""

    id: str
    member_id: str
    member_label: str
    action: str
    performed_by_email: str
    created_at: datetime


class GdprSummaryOut(BaseModel):
    """Headline counts for the Compliance tab -- how many members are
    live vs. already erased, and how much subject-request activity has
    happened recently."""

    total_members: int
    erased_members: int
    requests_last_30_days: int
