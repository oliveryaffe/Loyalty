"""Multi-location roll-up (competitive-brief backlog item #6) -- see
app/services/locations.py for the computation and app/db/models.py's
Location docstring for the data-model rationale.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Member


def _signup_and_login(client, business_name: str, email: str) -> dict:
    client.post(
        "/api/v1/auth/signup", json={"business_name": business_name, "email": email, "password": "s3cret-pw"}
    )
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": "s3cret-pw"})
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def _make_member(db_session, merchant_id, email, days_inactive=1, location_id=None):
    member = Member(
        merchant_id=merchant_id,
        first_name="Test",
        last_name="Member",
        email=email,
        points_balance=0,
        tier="bronze",
        last_activity_at=datetime.now(timezone.utc) - timedelta(days=days_inactive),
        location_id=location_id,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


@pytest.fixture()
def admin_headers(client):
    return _signup_and_login(client, "Locations Test Co", "locations-owner@acme.example.com")


def test_create_and_list_locations(client, admin_headers):
    resp = client.post("/api/v1/locations", json={"name": "High Street"}, headers=admin_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "High Street"

    list_resp = client.get("/api/v1/locations", headers=admin_headers)
    assert list_resp.status_code == 200
    names = [loc["name"] for loc in list_resp.json()]
    assert names == ["High Street"]


def test_create_location_rejects_blank_name(client, admin_headers):
    resp = client.post("/api/v1/locations", json={"name": "   "}, headers=admin_headers)
    assert resp.status_code == 400


def test_rollup_empty_when_no_locations_or_members(client, admin_headers):
    resp = client.get("/api/v1/locations/rollup", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_rollup_buckets_members_by_location(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    loc_a = client.post("/api/v1/locations", json={"name": "Location A"}, headers=admin_headers).json()
    loc_b = client.post("/api/v1/locations", json={"name": "Location B"}, headers=admin_headers).json()

    _make_member(db_session, merchant_id, "a1@example.com", days_inactive=1, location_id=loc_a["id"])
    _make_member(db_session, merchant_id, "a2@example.com", days_inactive=120, location_id=loc_a["id"])  # high risk
    _make_member(db_session, merchant_id, "b1@example.com", days_inactive=1, location_id=loc_b["id"])
    _make_member(db_session, merchant_id, "u1@example.com", days_inactive=1, location_id=None)

    resp = client.get("/api/v1/locations/rollup", headers=admin_headers)
    assert resp.status_code == 200
    rows = {r["name"]: r for r in resp.json()}

    assert rows["Location A"]["member_count"] == 2
    assert rows["Location A"]["high_risk_count"] == 1
    assert rows["Location B"]["member_count"] == 1
    assert rows["Location B"]["high_risk_count"] == 0
    assert rows["Unassigned"]["member_count"] == 1
    assert rows["Unassigned"]["location_id"] is None


def test_rollup_omits_unassigned_row_when_everyone_is_assigned(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    loc = client.post("/api/v1/locations", json={"name": "Only Shop"}, headers=admin_headers).json()
    _make_member(db_session, merchant_id, "assigned@example.com", location_id=loc["id"])

    resp = client.get("/api/v1/locations/rollup", headers=admin_headers)
    names = [r["name"] for r in resp.json()]
    assert "Unassigned" not in names


def test_assign_member_location(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    loc = client.post("/api/v1/locations", json={"name": "Assign Me"}, headers=admin_headers).json()
    member = _make_member(db_session, merchant_id, "assignable@example.com")

    resp = client.patch(
        f"/api/v1/members/{member.id}/location", json={"location_id": loc["id"]}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == loc["id"]

    db_session.refresh(member)
    assert member.location_id == loc["id"]


def test_assign_member_location_to_null_unassigns(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    loc = client.post("/api/v1/locations", json={"name": "Temp"}, headers=admin_headers).json()
    member = _make_member(db_session, merchant_id, "unassign@example.com", location_id=loc["id"])

    resp = client.patch(f"/api/v1/members/{member.id}/location", json={"location_id": None}, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json() is None

    db_session.refresh(member)
    assert member.location_id is None


def test_assign_member_location_rejects_unknown_location(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_member(db_session, merchant_id, "unknownloc@example.com")

    resp = client.patch(
        f"/api/v1/members/{member.id}/location", json={"location_id": "does-not-exist"}, headers=admin_headers
    )
    assert resp.status_code == 404


def test_locations_are_scoped_per_merchant(client, db_session):
    headers_a = _signup_and_login(client, "Merchant A", "merchant-a-loc@example.com")
    headers_b = _signup_and_login(client, "Merchant B", "merchant-b-loc@example.com")

    client.post("/api/v1/locations", json={"name": "A's Shop"}, headers=headers_a)
    loc_b = client.post("/api/v1/locations", json={"name": "B's Shop"}, headers=headers_b).json()

    list_a = client.get("/api/v1/locations", headers=headers_a).json()
    assert [loc["name"] for loc in list_a] == ["A's Shop"]

    # Merchant A cannot assign one of their members to Merchant B's location.
    merchant_a_id = _merchant_id(client, headers_a)
    member = _make_member(db_session, merchant_a_id, "cross-tenant@example.com")
    resp = client.patch(
        f"/api/v1/members/{member.id}/location", json={"location_id": loc_b["id"]}, headers=headers_a
    )
    assert resp.status_code == 404
