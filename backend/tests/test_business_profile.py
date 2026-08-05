"""Onboarding business-type picker (Merchant.business_type): the
/settings/business-types and /settings/business-profile endpoints, plus
the calibration fallback it feeds (app.ai.churn_model).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.ai.churn_model import (
    BUSINESS_TYPE_CALIBRATIONS,
    DEFAULT_CALIBRATION,
    compute_merchant_calibration,
)
from app.db.models import Member, Merchant


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Acme Retail", "email": "profile-owner@acme.example.com", "password": "s3cret-pw"},
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "profile-owner@acme.example.com", "password": "s3cret-pw"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_business_types_list_includes_expected_verticals(client, admin_headers):
    resp = client.get("/api/v1/settings/business-types", headers=admin_headers)
    assert resp.status_code == 200
    values = {opt["value"] for opt in resp.json()}
    assert {"coffee_shop", "restaurant", "barber_salon", "retail", "other"} <= values


def test_business_profile_defaults_to_null_business_type(client, admin_headers):
    resp = client.get("/api/v1/settings/business-profile", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["business_type"] is None
    # No business_type and no history yet -> generic default calibration.
    assert resp.json()["calibration_source"] == "default"


def test_business_profile_update_persists(client, admin_headers):
    resp = client.patch(
        "/api/v1/settings/business-profile", json={"business_type": "barber_salon"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["business_type"] == "barber_salon"
    # Thin history + a real business_type -> vertical-default calibration,
    # surfaced so the frontend can show which mode is active.
    assert resp.json()["calibration_source"] == "default_vertical"

    refetched = client.get("/api/v1/settings/business-profile", headers=admin_headers)
    assert refetched.json()["business_type"] == "barber_salon"
    assert refetched.json()["calibration_source"] == "default_vertical"


def test_business_profile_rejects_unknown_business_type(client, admin_headers):
    resp = client.patch(
        "/api/v1/settings/business-profile", json={"business_type": "space-station"}, headers=admin_headers
    )
    assert resp.status_code == 400


def test_business_profile_update_requires_admin_role(client, admin_headers):
    client.post(
        "/api/v1/team/invite",
        json={"email": "profile-teammate@acme.example.com", "password": "teammate-pw1", "role": "member"},
        headers=admin_headers,
    )
    login = client.post(
        "/api/v1/auth/login", json={"email": "profile-teammate@acme.example.com", "password": "teammate-pw1"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.patch(
        "/api/v1/settings/business-profile", json={"business_type": "retail"}, headers=member_headers
    )
    assert resp.status_code == 403

    # Reads stay open to any team member.
    assert client.get("/api/v1/settings/business-profile", headers=member_headers).status_code == 200


# ---------------------------------------------------------------------------
# Calibration fallback
# ---------------------------------------------------------------------------


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def test_calibration_falls_back_to_default_when_business_type_unset(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    # No members at all -> well under MIN_MEMBERS_WITH_REPEAT_VISITS.
    calibration = compute_merchant_calibration(db_session, merchant_id)
    assert calibration.source == "default"
    assert calibration == DEFAULT_CALIBRATION


def test_calibration_uses_vertical_profile_when_business_type_set_and_history_thin(
    client, db_session, admin_headers
):
    merchant_id = _merchant_id(client, admin_headers)
    client.patch("/api/v1/settings/business-profile", json={"business_type": "barber_salon"}, headers=admin_headers)

    calibration = compute_merchant_calibration(db_session, merchant_id)
    assert calibration.source == "default_vertical"
    assert calibration == BUSINESS_TYPE_CALIBRATIONS["barber_salon"]


def test_calibration_falls_back_to_default_for_other_business_type(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    client.patch("/api/v1/settings/business-profile", json={"business_type": "other"}, headers=admin_headers)

    calibration = compute_merchant_calibration(db_session, merchant_id)
    assert calibration.source == "default"
    assert calibration == DEFAULT_CALIBRATION


def test_calibration_ignores_business_type_once_real_history_exists(client, db_session, admin_headers):
    """The vertical fallback only applies while there's too little history
    to calibrate for real -- once a merchant has enough repeat-visit data,
    business_type must not override the real, measured calibration."""
    from app.db.models import Transaction, TransactionType

    merchant_id = _merchant_id(client, admin_headers)
    merchant = db_session.get(Merchant, merchant_id)
    merchant.business_type = "retail"
    db_session.commit()

    now = datetime.now(timezone.utc)
    for i in range(25):
        member = Member(
            merchant_id=merchant_id,
            first_name=f"Regular{i}",
            last_name="Customer",
            email=f"regular{i}@example.com",
            points_balance=0,
            last_activity_at=now - timedelta(days=2),
        )
        db_session.add(member)
        db_session.flush()
        for offset in (30, 20, 10, 2):
            db_session.add(
                Transaction(
                    member_id=member.id,
                    type=TransactionType.EARN.value,
                    amount_gbp=8.0,
                    points=8,
                    channel="pos",
                    created_at=now - timedelta(days=offset),
                )
            )
    db_session.commit()

    calibration = compute_merchant_calibration(db_session, merchant_id, now=now)
    assert calibration.source == "calibrated"
