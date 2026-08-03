"""Member CRUD + list (scoped to the authenticated merchant)."""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.ai.churn_model import score_member_churn
from app.api.deps import require_active_subscription, require_admin
from app.db.base import get_db
from app.db.models import (
    ExperimentAssignment,
    FraudAlert,
    Member,
    Merchant,
    Redemption,
    TeamMember,
    Transaction,
    WinbackOffer,
)
from app.schemas.gdpr import MemberErasureResult, MemberExportOut
from app.schemas.member import MemberCreate, MemberOut, MemberWithChurn

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
    for m in members:
        out = MemberWithChurn.model_validate(m)
        if include_churn:
            churn = score_member_churn(db, m)
            out.churn_risk_score = churn.churn_risk_score
            out.churn_risk_band = churn.risk_band
        results.append(out)
    return results


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
    churn = score_member_churn(db, member)
    out.churn_risk_score = churn.churn_risk_score
    out.churn_risk_band = churn.risk_band
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
        return MemberErasureResult(
            member_id=member.id, erased_at=member.erased_at, already_erased=True
        )

    member.first_name = "Erased"
    member.last_name = "Member"
    member.email = f"erased-{member.id}@deleted.ledgerly.invalid"
    member.is_active = False
    member.erased_at = datetime.now(timezone.utc)
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
    winback_offers = db.query(WinbackOffer).filter(WinbackOffer.member_id == member.id).all()
    experiment_assignments = (
        db.query(ExperimentAssignment).filter(ExperimentAssignment.member_id == member.id).all()
    )

    return MemberExportOut(
        member=member,
        transactions=transactions,
        redemptions=redemptions,
        fraud_alerts=fraud_alerts,
        winback_offers=winback_offers,
        experiment_assignments=experiment_assignments,
        exported_at=datetime.now(timezone.utc),
    )
