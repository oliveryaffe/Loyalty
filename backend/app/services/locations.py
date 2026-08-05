"""Multi-location roll-up (competitive-brief backlog item #6): "once
single-location product-market fit is proven, a lightweight 'view across
your N shops' mode is a natural expansion axis." Deliberately scoped to a
roll-up summary rather than making every existing report/endpoint
location-aware -- see app/db/models.py::Location for the data-model
rationale. A merchant with one shop (the common case today) never
interacts with any of this; Location rows are entirely opt-in.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.ai.churn_model import compute_merchant_calibration, score_all_members
from app.ai.future_value import score_all_members_future_value
from app.db.models import Location, Member

UNASSIGNED_LABEL = "Unassigned"


@dataclass(frozen=True)
class LocationRollupRow:
    location_id: str | None
    name: str
    member_count: int
    high_risk_count: int
    predicted_value_90d: float


def compute_location_rollup(db: Session, merchant_id: str) -> list[LocationRollupRow]:
    """One row per Location the merchant has created, plus a trailing
    "Unassigned" row for any member with no location_id -- the rollup
    always accounts for every member, never silently drops the ones that
    predate this feature. Computes churn/future-value once for the whole
    merchant (same "calibrate once per request" pattern as every other
    batch scoring call site) and buckets the results by location in
    Python, rather than re-deriving calibration once per location."""
    locations = db.query(Location).filter(Location.merchant_id == merchant_id).order_by(Location.name).all()
    location_name_by_id = {loc.id: loc.name for loc in locations}

    calibration = compute_merchant_calibration(db, merchant_id)
    churn_results = score_all_members(db, merchant_id, calibration=calibration)
    future_value_results = score_all_members_future_value(db, merchant_id)

    member_location = {
        m.id: m.location_id
        for m in db.query(Member.id, Member.location_id).filter(Member.merchant_id == merchant_id).all()
    }

    member_counts: dict[str | None, int] = {}
    high_risk_counts: dict[str | None, int] = {}
    predicted_values: dict[str | None, float] = {}

    for r in churn_results:
        loc_id = member_location.get(r.member_id)
        member_counts[loc_id] = member_counts.get(loc_id, 0) + 1
        if r.risk_band == "high":
            high_risk_counts[loc_id] = high_risk_counts.get(loc_id, 0) + 1

    for r in future_value_results:
        loc_id = member_location.get(r.member_id)
        predicted_values[loc_id] = predicted_values.get(loc_id, 0.0) + r.predicted_value

    rows: list[LocationRollupRow] = []
    for loc in locations:
        rows.append(
            LocationRollupRow(
                location_id=loc.id,
                name=loc.name,
                member_count=member_counts.get(loc.id, 0),
                high_risk_count=high_risk_counts.get(loc.id, 0),
                predicted_value_90d=round(predicted_values.get(loc.id, 0.0), 2),
            )
        )

    # Unassigned row -- only included if it's non-empty, so a merchant who
    # has assigned every member to a location isn't shown a pointless
    # "Unassigned: 0" row forever.
    unassigned_count = member_counts.get(None, 0)
    if unassigned_count > 0:
        rows.append(
            LocationRollupRow(
                location_id=None,
                name=UNASSIGNED_LABEL,
                member_count=unassigned_count,
                high_risk_count=high_risk_counts.get(None, 0),
                predicted_value_90d=round(predicted_values.get(None, 0.0), 2),
            )
        )

    return rows
