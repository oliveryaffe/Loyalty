"""Team management: invite/list/remove teammates, role gating.

Read access (`GET /team`) requires an active subscription via
`require_active_subscription` (same convention as every other paid-tier
router in this batch -- app/api/winback.py, app/api/experiments.py). Write
endpoints (invite/remove/role-change) require both the `admin` role AND an
active subscription via `require_admin_active_subscription`, so a
cancelled/unpaid merchant admin can no longer invite or remove teammates
indefinitely (TEST_REPORT_BATCH3.md §1 -- this router was the one gap in an
otherwise-consistent gating pass). All write endpoints scope lookups to the
caller's own `merchant_id` (no cross-merchant IDOR) and guard against ever
leaving a merchant with zero active admins (the "last-admin lockout" rule).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import require_active_subscription, require_admin_active_subscription
from app.db.base import get_db
from app.db.models import Merchant, TeamMember, TeamRole
from app.schemas.auth import RoleUpdate, TeamMemberCreate, TeamMemberOut
from app.services.security import hash_password

router = APIRouter(prefix="/api/v1/team", tags=["team"])


def _active_admin_count(db: Session, merchant_id: str) -> int:
    return (
        db.query(TeamMember)
        .filter(
            TeamMember.merchant_id == merchant_id,
            TeamMember.role == TeamRole.ADMIN.value,
            TeamMember.is_active.is_(True),
        )
        .count()
    )


def _get_team_member_or_404(db: Session, merchant_id: str, team_member_id: str) -> TeamMember:
    target = (
        db.query(TeamMember)
        .filter(TeamMember.id == team_member_id, TeamMember.merchant_id == merchant_id)
        .first()
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Team member not found")
    return target


@router.get("", response_model=list[TeamMemberOut])
def list_team(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> list[TeamMember]:
    return (
        db.query(TeamMember)
        .filter(TeamMember.merchant_id == merchant.id)
        .order_by(TeamMember.created_at.asc())
        .all()
    )


@router.post("/invite", response_model=TeamMemberOut, status_code=status.HTTP_201_CREATED)
def invite_team_member(
    payload: TeamMemberCreate,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> TeamMember:
    existing = db.query(TeamMember).filter(TeamMember.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    new_member = TeamMember(
        merchant_id=merchant.id,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    db.add(new_member)
    db.commit()
    db.refresh(new_member)
    return new_member


@router.delete("/{team_member_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_team_member(
    team_member_id: str,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> None:
    target = _get_team_member_or_404(db, merchant.id, team_member_id)

    if target.role == TeamRole.ADMIN.value and target.is_active:
        if _active_admin_count(db, merchant.id) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot remove the merchant's last remaining admin",
            )

    db.delete(target)
    db.commit()
    return None


@router.patch("/{team_member_id}/role", response_model=TeamMemberOut)
def update_team_member_role(
    team_member_id: str,
    payload: RoleUpdate,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> TeamMember:
    target = _get_team_member_or_404(db, merchant.id, team_member_id)

    is_demotion = target.role == TeamRole.ADMIN.value and payload.role != TeamRole.ADMIN.value
    if is_demotion and target.is_active:
        if _active_admin_count(db, merchant.id) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot demote the merchant's last remaining admin",
            )

    target.role = payload.role
    db.commit()
    db.refresh(target)
    return target
