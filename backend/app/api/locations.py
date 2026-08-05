"""Multi-location roll-up (competitive-brief backlog item #6) -- see
app/services/locations.py for the computation and app/db/models.py's
Location docstring for the data-model rationale. Gated the same as
members.py (require_active_subscription, no admin requirement --
creating/assigning a location is everyday data organisation, not a
destructive or billing-affecting action, consistent with how member
creation itself is gated)."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import require_active_subscription
from app.db.base import get_db
from app.db.models import Location, Member, Merchant
from app.schemas.location import LocationCreate, LocationOut, LocationRollupOut, MemberLocationUpdate
from app.services.locations import compute_location_rollup

router = APIRouter(tags=["locations"])


@router.get("/api/v1/locations", response_model=list[LocationOut])
def list_locations(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> list[Location]:
    return db.query(Location).filter(Location.merchant_id == merchant.id).order_by(Location.name).all()


@router.post("/api/v1/locations", response_model=LocationOut, status_code=status.HTTP_201_CREATED)
def create_location(
    payload: LocationCreate,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> Location:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Location name cannot be blank")
    location = Location(merchant_id=merchant.id, name=name)
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


@router.get("/api/v1/locations/rollup", response_model=list[LocationRollupOut])
def get_location_rollup(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> list[LocationRollupOut]:
    rows = compute_location_rollup(db, merchant.id)
    return [
        LocationRollupOut(
            location_id=r.location_id,
            name=r.name,
            member_count=r.member_count,
            high_risk_count=r.high_risk_count,
            predicted_value_90d=r.predicted_value_90d,
        )
        for r in rows
    ]


@router.patch("/api/v1/members/{member_id}/location", response_model=LocationOut | None)
def assign_member_location(
    member_id: str,
    payload: MemberLocationUpdate,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> Location | None:
    member = db.query(Member).filter(Member.id == member_id, Member.merchant_id == merchant.id).first()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    if payload.location_id is None:
        member.location_id = None
        db.commit()
        return None

    location = (
        db.query(Location)
        .filter(Location.id == payload.location_id, Location.merchant_id == merchant.id)
        .first()
    )
    if location is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")

    member.location_id = location.id
    db.commit()
    return location
