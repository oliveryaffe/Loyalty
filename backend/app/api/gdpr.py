"""Compliance tab: cross-member GDPR surfaces that don't belong on the
per-member endpoints in app/api/members.py (which own the actual
export/erasure actions and write the audit log entries this router
reads). This router is the "show your work" half of GDPR compliance --
a summary of where the account stands, and a chronological record of
every subject-access export and erasure request.

Gated on `require_admin` only (not `require_active_subscription`), same
reasoning as the erase/export endpoints themselves: a GDPR compliance
obligation doesn't pause because an invoice is unpaid.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.db.base import get_db
from app.db.models import GdprAuditLogEntry, Member, TeamMember
from app.schemas.gdpr import GdprAuditLogEntryOut, GdprSummaryOut

router = APIRouter(prefix="/api/v1/gdpr", tags=["gdpr"])

DEFAULT_AUDIT_LOG_LIMIT = 50
SUMMARY_WINDOW_DAYS = 30


@router.get("/summary", response_model=GdprSummaryOut)
def get_gdpr_summary(
    db: Session = Depends(get_db),
    current_user: TeamMember = Depends(require_admin),
) -> GdprSummaryOut:
    merchant_id = current_user.merchant_id
    total_members = db.query(Member).filter(Member.merchant_id == merchant_id).count()
    erased_members = (
        db.query(Member)
        .filter(Member.merchant_id == merchant_id, Member.erased_at.is_not(None))
        .count()
    )
    since = datetime.now(timezone.utc) - timedelta(days=SUMMARY_WINDOW_DAYS)
    requests_last_30_days = (
        db.query(GdprAuditLogEntry)
        .filter(GdprAuditLogEntry.merchant_id == merchant_id, GdprAuditLogEntry.created_at >= since)
        .count()
    )
    return GdprSummaryOut(
        total_members=total_members,
        erased_members=erased_members,
        requests_last_30_days=requests_last_30_days,
    )


@router.get("/audit-log", response_model=list[GdprAuditLogEntryOut])
def get_gdpr_audit_log(
    limit: int = Query(DEFAULT_AUDIT_LOG_LIMIT, gt=0, le=200),
    db: Session = Depends(get_db),
    current_user: TeamMember = Depends(require_admin),
) -> list[GdprAuditLogEntryOut]:
    entries = (
        db.query(GdprAuditLogEntry)
        .filter(GdprAuditLogEntry.merchant_id == current_user.merchant_id)
        .order_by(GdprAuditLogEntry.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        GdprAuditLogEntryOut(
            id=e.id,
            member_id=e.member_id,
            member_label=e.member_label,
            action=e.action,
            performed_by_email=e.performed_by_email,
            created_at=e.created_at,
        )
        for e in entries
    ]
