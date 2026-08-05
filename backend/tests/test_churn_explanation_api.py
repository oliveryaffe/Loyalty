"""Plain-English "why" explanations surfaced through the API (competitive-
brief backlog item #5) -- see app/ai/churn_model.py::explain_churn_risk
for the underlying logic, tested directly in tests/test_churn_model.py.
This file just checks the field actually reaches GET /ai/churn and
GET /members.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Member


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Explain Test Co", "email": "explain-owner@acme.example.com", "password": "s3cret-pw"},
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "explain-owner@acme.example.com", "password": "s3cret-pw"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def _make_stale_member(db_session, merchant_id):
    member = Member(
        merchant_id=merchant_id,
        first_name="Stale",
        last_name="Member",
        email="stale-explain@example.com",
        points_balance=0,
        tier="bronze",
        last_activity_at=datetime.now(timezone.utc) - timedelta(days=120),
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


def test_churn_endpoint_includes_explanation(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert resp.status_code == 200
    row = next(r for r in resp.json() if r["member_id"] == member.id)
    assert row["risk_band"] == "high"
    assert "explanation" in row
    assert row["explanation"].startswith("High risk")


def test_member_detail_endpoint_includes_explanation(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_stale_member(db_session, merchant_id)

    resp = client.get(f"/api/v1/members/{member.id}", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["churn_risk_explanation"]
    assert body["churn_risk_explanation"].startswith("High risk")


def test_members_list_includes_explanation_when_churn_requested(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/members?include_churn=true", headers=admin_headers)
    assert resp.status_code == 200
    row = next(m for m in resp.json() if m["id"] == member.id)
    assert row["churn_risk_explanation"]


def test_members_list_omits_explanation_when_churn_not_requested(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/members?include_churn=false", headers=admin_headers)
    assert resp.status_code == 200
    row = next(m for m in resp.json() if m["id"] == member.id)
    assert row["churn_risk_explanation"] is None
