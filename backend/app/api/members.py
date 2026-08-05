"""Member CRUD + list (scoped to the authenticated merchant)."""
import csv
import io
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.ai.churn_model import compute_merchant_calibration, score_member_churn
from app.ai.next_visit import predict_next_visit_for_all_members
from app.api.deps import require_active_subscription, require_admin
from app.db.base import get_db
from app.db.models import (
    ExperimentAssignment,
    FraudAlert,
    GdprAuditLogEntry,
    Member,
    Merchant,
    Redemption,
    TeamMember,
    Transaction,
)
from app.schemas.gdpr import MemberErasureResult, MemberExportOut
from app.schemas.member import MemberCreate, MemberOut, MemberWithChurn
from app.services.audience_export import (
    AUDIENCE_EXPORT_FORMATS,
    DEFAULT_EXPORT_FORMAT,
    VALID_RISK_BANDS,
    build_audience_export_rows,
    build_next_best_export_rows,
)
from app.services.usage import record_usage_event

router = APIRouter(prefix="/api/v1/members", tags=["members"])

logger = logging.getLogger(__name__)


@router.get("", response_model=list[MemberWithChurn])
def list_members(
    include_churn: bool = True,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> list[MemberWithChurn]:
    members = db.query(Member).filter(Member.merchant_id == merchant.id).all()
    results: list[MemberWithChurn] = []
    # Calibration is derived from this merchant's own transaction history
    # (see churn_model.py) -- compute it once per request, not once per
    # member in the loop below.
    calibration = compute_merchant_calibration(db, merchant.id) if include_churn else None
    # Computed once for the whole merchant, same "don't recompute a
    # merchant-wide aggregate per member" shape as calibration above --
    # see app/ai/next_visit.py.
    next_visits = {p.member_id: p for p in predict_next_visit_for_all_members(db, merchant.id)}
    for m in members:
        out = MemberWithChurn.model_validate(m)
        if include_churn:
            churn = score_member_churn(db, m, calibration=calibration)
            out.churn_risk_score = churn.churn_risk_score
            out.churn_risk_band = churn.risk_band
            out.churn_risk_explanation = churn.explanation
        next_visit = next_visits.get(m.id)
        if next_visit is not None:
            out.predicted_next_visit_date = next_visit.predicted_next_visit_date
            out.next_visit_days_overdue = next_visit.days_overdue
        results.append(out)
    return results


@router.get("/export.csv")
def export_audience(
    risk: str | None = Query(None),
    next_best_category: str | None = Query(None),
    format: str = Query(DEFAULT_EXPORT_FORMAT),
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> StreamingResponse:
    """Actionable audience export (competitive-brief backlog item #4) --
    see app/services/audience_export.py for the full rationale. Declared
    before GET /{member_id} below so "export.csv" is matched as this
    literal route rather than captured as a member_id path parameter.

    Exactly one of `risk` ("high"/"medium"/"low") or `next_best_category`
    may be set to filter the export; omitting both exports every member.
    `format` picks the header row to match Mailchimp, Klaviyo, or a plain
    generic CSV -- same underlying rows either way.
    """
    if risk is not None and next_best_category is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at most one of risk or next_best_category, not both",
        )
    if risk is not None and risk not in VALID_RISK_BANDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"risk must be one of {VALID_RISK_BANDS}"
        )
    if format not in AUDIENCE_EXPORT_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"format must be one of {sorted(AUDIENCE_EXPORT_FORMATS)}",
        )

    if next_best_category is not None:
        rows = build_next_best_export_rows(db, merchant.id, next_best_category)
        segment_label = f"next-best-{next_best_category}"
    else:
        rows = build_audience_export_rows(db, merchant.id, risk_band=risk)
        segment_label = risk or "all"

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(AUDIENCE_EXPORT_FORMATS[format])
    writer.writerows(rows)
    buffer.seek(0)

    # Billable insight run (app/services/usage.py) -- same reasoning as
    # GET /insights/report.csv: a real, merchant-initiated export of
    # generated output, not a passive dashboard read.
    record_usage_event(db, merchant, "audience_export")
    db.commit()

    filename = f"ledgerly-audience-{segment_label}-{format}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{member_id}", response_model=MemberWithChurn)
def get_member(
    member_id: str,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> MemberWithChurn:
    member = db.query(Member).filter(Member.id == member_id, Member.merchant_id == merchant.id).first()
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    out = MemberWithChurn.model_validate(member)
    calibration = compute_merchant_calibration(db, merchant.id)
    churn = score_member_churn(db, member, calibration=calibration)
    out.churn_risk_score = churn.churn_risk_score
    out.churn_risk_band = churn.risk_band
    out.churn_risk_explanation = churn.explanation

    # Single-member call site -- reuses the same merchant-wide fallback
    # computation as the list endpoint, just scoped to one member's
    # response instead of building the whole dict (see
    # app/ai/next_visit.py's docstring for the fallback rationale).
    all_next_visits = predict_next_visit_for_all_members(db, merchant.id)
    next_visit = next((p for p in all_next_visits if p.member_id == member.id), None)
    if next_visit is not None:
        out.predicted_next_visit_date = next_visit.predicted_next_visit_date
        out.next_visit_days_overdue = next_visit.days_overdue
    return out


@router.post("", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
def create_member(
    payload: MemberCreate,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> Member:
    member = Member(
        merchant_id=merchant.id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=payload.email,
        tier=payload.tier,
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return member


def _get_member_or_404(db: Session, merchant_id: str, member_id: str) -> Member:
    member = (
        db.query(Member)
        .filter(Member.id == member_id, Member.merchant_id == merchant_id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return member


def _log_gdpr_action(
    db: Session, current_user: TeamMember, member: Member, action: str, label: str
) -> None:
    """Records one row in the Compliance tab's audit trail
    (app.db.models.GdprAuditLogEntry). `label` is frozen at call time
    (the member's current name/email) rather than re-derived later, so
    the audit trail still reads correctly even after a subsequent
    erasure overwrites the member's name/email fields."""
    db.add(
        GdprAuditLogEntry(
            merchant_id=current_user.merchant_id,
            member_id=member.id,
            member_label=label,
            action=action,
            performed_by_team_member_id=current_user.id,
            performed_by_email=current_user.email,
        )
    )


@router.post("/{member_id}/gdpr-erase", response_model=MemberErasureResult)
def gdpr_erase_member(
    member_id: str,
    db: Session = Depends(get_db),
    current_user: TeamMember = Depends(require_admin),
) -> MemberErasureResult:
    """UK GDPR right to erasure (Art. 17) -- PLAN_BATCH3.md §1a.

    Deliberately anonymizes rather than hard-deletes: `Member` has
    `cascade="all, delete-orphan"` relationships to `Transaction` and
    `Redemption` (app/db/models.py), so a literal `db.delete(member)` would
    silently destroy the merchant's own business records (revenue figures,
    churn/future-value training data, fraud-alert history) every time a
    member is erased. Instead this overwrites the directly-identifying
    fields (name, email) with non-identifying placeholders, deactivates the
    member, and stamps `erased_at` -- the row and every FK'd
    Transaction/Redemption/FraudAlert stay intact, attributed to a stable
    but now-anonymous id.

    Gated on `require_admin` (stricter than `require_active_subscription`,
    used by every other member endpoint above) because this action is
    compliance-sensitive and irreversible. Deliberately NOT gated on
    `require_active_subscription` either -- see PLAN_BATCH3.md §2: a GDPR
    erasure/export obligation doesn't pause because an invoice is unpaid,
    so this stays reachable even for a hard-locked (canceled/unpaid/no-sub)
    merchant.

    Idempotent: calling this twice on an already-erased member is a no-op
    that returns the existing state with `already_erased=True`, rather than
    erroring or re-stamping `erased_at`.
    """
    member = _get_member_or_404(db, current_user.merchant_id, member_id)

    if member.erased_at is not None:
        _log_gdpr_action(db, current_user, member, "erase", f"{member.first_name} {member.last_name}")
        db.commit()
        return MemberErasureResult(
            member_id=member.id, erased_at=member.erased_at, already_erased=True
        )

    # Frozen before the overwrite below -- the audit trail should still
    # read "erased jane.doe@example.com", not "erased Erased Member".
    original_label = f"{member.first_name} {member.last_name} <{member.email}>"

    member.first_name = "Erased"
    member.last_name = "Member"
    member.email = f"erased-{member.id}@deleted.ledgerly.invalid"
    member.is_active = False
    member.erased_at = datetime.now(timezone.utc)
    _log_gdpr_action(db, current_user, member, "erase", original_label)
    db.commit()
    db.refresh(member)

    logger.info("GDPR erasure: admin=%s member=%s", current_user.id, member.id)

    return MemberErasureResult(
        member_id=member.id, erased_at=member.erased_at, already_erased=False
    )


@router.get("/{member_id}/gdpr-export", response_model=MemberExportOut)
def gdpr_export_member(
    member_id: str,
    db: Session = Depends(get_db),
    current_user: TeamMember = Depends(require_admin),
) -> MemberExportOut:
    """Combined UK GDPR subject-access (Art. 15) + portability (Art. 20)
    export -- PLAN_BATCH3.md §1b. One machine-readable JSON payload covers
    both regimes, a pragmatic MVP framing flagged in the plan as a legal
    call for a solicitor to confirm, not asserted here as definitively
    compliant.

    Gated on `require_admin` for the same reason as erasure above: a
    full-PII export is sensitive enough to warrant the stricter gate.

    404 if the member doesn't exist or belongs to a different merchant
    (matches every other member-scoped lookup in this codebase). 410 Gone
    if the member has already been erased -- there is no personal data
    left to export, and returning a near-empty payload would be confusing
    rather than informative.
    """
    member = _get_member_or_404(db, current_user.merchant_id, member_id)

    if member.erased_at is not None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Member has been erased; no personal data remains to export",
        )

    transactions = db.query(Transaction).filter(Transaction.member_id == member.id).all()
    redemptions = db.query(Redemption).filter(Redemption.member_id == member.id).all()
    fraud_alerts = db.query(FraudAlert).filter(FraudAlert.member_id == member.id).all()
    experiment_assignments = (
        db.query(ExperimentAssignment).filter(ExperimentAssignment.member_id == member.id).all()
    )

    _log_gdpr_action(db, current_user, member, "export", f"{member.first_name} {member.last_name} <{member.email}>")
    db.commit()

    return MemberExportOut(
        member=member,
        transactions=transactions,
        redemptions=redemptions,
        fraud_alerts=fraud_alerts,
        experiment_assignments=experiment_assignments,
        exported_at=datetime.now(timezone.utc),
    )
