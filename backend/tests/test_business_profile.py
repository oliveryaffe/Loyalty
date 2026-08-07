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


# ---------------------------------------------------------------------------
# POST /settings/business-profile/reset
# ---------------------------------------------------------------------------


def test_reset_clears_business_type_back_to_null(client, admin_headers):
    client.patch("/api/v1/settings/business-profile", json={"business_type": "retail"}, headers=admin_headers)

    resp = client.post("/api/v1/settings/business-profile/reset", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["business_type"] is None
    assert resp.json()["calibration_source"] == "default"

    refetched = client.get("/api/v1/settings/business-profile", headers=admin_headers)
    assert refetched.json()["business_type"] is None


def test_reset_requires_admin_role(client, admin_headers):
    client.patch("/api/v1/settings/business-profile", json={"business_type": "retail"}, headers=admin_headers)
    client.post(
        "/api/v1/team/invite",
        json={"email": "profile-reset-teammate@acme.example.com", "password": "teammate-pw1", "role": "member"},
        headers=admin_headers,
    )
    login = client.post(
        "/api/v1/auth/login", json={"email": "profile-reset-teammate@acme.example.com", "password": "teammate-pw1"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post("/api/v1/settings/business-profile/reset", headers=member_headers)
    assert resp.status_code == 403

    # business_type is unchanged after the rejected reset attempt.
    assert client.get("/api/v1/settings/business-profile", headers=admin_headers).json()["business_type"] == "retail"


def test_reset_is_a_noop_when_already_unset(client, admin_headers):
    resp = client.post("/api/v1/settings/business-profile/reset", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["business_type"] is None


# ---------------------------------------------------------------------------
# Customer data source (second onboarding question)
# ---------------------------------------------------------------------------


def test_data_sources_list_includes_expected_options(client, admin_headers):
    resp = client.get("/api/v1/settings/data-sources", headers=admin_headers)
    assert resp.status_code == 200
    values = {opt["value"] for opt in resp.json()}
    assert values == {"loyalty_app", "booking_app", "checkout_or_online", "esp_list", "none"}


def test_none_option_carries_a_hint_other_options_do_not(client, admin_headers):
    options = client.get("/api/v1/settings/data-sources", headers=admin_headers).json()
    by_value = {opt["value"]: opt for opt in options}
    assert by_value["none"]["hint"] is not None
    assert "Ledgerly reads your existing customer data" in by_value["none"]["hint"]
    assert by_value["loyalty_app"]["hint"] is None


def test_customer_data_source_defaults_to_none_and_can_be_set(client, admin_headers):
    profile = client.get("/api/v1/settings/business-profile", headers=admin_headers).json()
    assert profile["customer_data_source"] is None

    resp = client.patch(
        "/api/v1/settings/customer-data-source", json={"value": "booking_app"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["customer_data_source"] == "booking_app"

    refetched = client.get("/api/v1/settings/business-profile", headers=admin_headers)
    assert refetched.json()["customer_data_source"] == "booking_app"


def test_customer_data_source_rejects_invalid_value(client, admin_headers):
    resp = client.patch(
        "/api/v1/settings/customer-data-source", json={"value": "carrier_pigeon"}, headers=admin_headers
    )
    assert resp.status_code == 400


def test_customer_data_source_does_not_gate_any_feature(client, admin_headers):
    """Answering "none" is purely informational -- the merchant can still
    use the product exactly as before (e.g. list members, which returns
    empty rather than erroring)."""
    resp = client.patch("/api/v1/settings/customer-data-source", json={"value": "none"}, headers=admin_headers)
    assert resp.status_code == 200

    members_resp = client.get("/api/v1/members", headers=admin_headers)
    assert members_resp.status_code == 200


def test_business_profile_reset_does_not_clear_data_source(client, admin_headers):
    client.patch("/api/v1/settings/customer-data-source", json={"value": "esp_list"}, headers=admin_headers)
    client.patch("/api/v1/settings/business-profile", json={"business_type": "retail"}, headers=admin_headers)

    resp = client.post("/api/v1/settings/business-profile/reset", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["business_type"] is None
    assert resp.json()["customer_data_source"] == "esp_list"
