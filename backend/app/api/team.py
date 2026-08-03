"""Team management: invite/list/remove teammates, role gating.

Read access (`GET /team`) is open to any authenticated team member of the
merchant. Write endpoints (invite/remove/role-change) require the `admin`
role via `require_admin`. All write endpoints scope lookups to the caller's
own `merchant_id` (no cross-merchant IDOR) and guard against ever leaving a
merchant with zero active admins (the "last-admin lockout" rule).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.db.base import get_db
from app.db.models import TeamMember, TeamRole
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
    current_user: TeamMember = Depends(get_current_user),
) -> list[TeamMember]:
    return (
        db.query(TeamMember)
        .filter(TeamMember.merchant_id == current_user.merchant_id)
        .order_by(TeamMember.created_at.asc())
        .all()
    )


@router.post("/invite", response_model=TeamMemberOut, status_code=status.HTTP_201_CREATED)
def invite_team_member(
    payload: TeamMemberCreate,
    db: Session = Depends(get_db),
    current_user: TeamMember = Depends(require_admin),
) -> TeamMember:
    existing = db.query(TeamMember).filter(TeamMember.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    new_member = TeamMember(
        merchant_id=current_user.merchant_id,
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
    current_user: TeamMember = Depends(require_admin),
) -> None:
    target = _get_team_member_or_404(db, current_user.merchant_id, team_member_id)

    if target.role == TeamRole.ADMIN.value and target.is_active:
        if _active_admin_count(db, current_user.merchant_id) <= 1:
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
    current_user: TeamMember = Depends(require_admin),
) -> TeamMember:
    target = _get_team_member_or_404(db, current_user.merchant_id, team_member_id)

    is_demotion = target.role == TeamRole.ADMIN.value and payload.role != TeamRole.ADMIN.value
    if is_demotion and target.is_active:
        if _active_admin_count(db, current_user.merchant_id) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot demote the merchant's last remaining admin",
            )

    target.role = payload.role
    db.commit()
    db.refresh(target)
    return target
