"""GDPR technical pass (PLAN_BATCH3.md §1): member erasure (anonymize, not
hard-delete) and the combined subject-access/portability export.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import ExperimentAssignment, Member


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Acme Retail", "email": "owner@acme.example.com", "password": "s3cret-pw"},
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "owner@acme.example.com", "password": "s3cret-pw"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def member_role_headers(client, admin_headers):
    client.post(
        "/api/v1/team/invite",
        json={"email": "teammate@acme.example.com", "password": "teammate-pw1", "role": "member"},
        headers=admin_headers,
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "teammate@acme.example.com", "password": "teammate-pw1"},
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_member(client, headers, email="grace@example.com"):
    resp = client.post(
        "/api/v1/members",
        json={"first_name": "Grace", "last_name": "Hopper", "email": email},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Erasure
# ---------------------------------------------------------------------------


def test_gdpr_erase_anonymizes_member_and_deactivates(client, admin_headers):
    member_id = _create_member(client, admin_headers)

    erase_resp = client.post(f"/api/v1/members/{member_id}/gdpr-erase", headers=admin_headers)
    assert erase_resp.status_code == 200
    body = erase_resp.json()
    assert body["member_id"] == member_id
    assert body["already_erased"] is False
    assert body["erased_at"] is not None

    get_resp = client.get(f"/api/v1/members/{member_id}", headers=admin_headers)
    assert get_resp.status_code == 200
    member = get_resp.json()
    assert member["first_name"] == "Erased"
    assert member["last_name"] == "Member"
    assert member["email"] == f"erased-{member_id}@deleted.ledgerly.invalid"
    assert member["is_active"] is False
    assert member["erased_at"] is not None


def test_gdpr_erase_is_idempotent(client, admin_headers):
    member_id = _create_member(client, admin_headers)

    first = client.post(f"/api/v1/members/{member_id}/gdpr-erase", headers=admin_headers)
    assert first.json()["already_erased"] is False
    first_erased_at = first.json()["erased_at"]

    second = client.post(f"/api/v1/members/{member_id}/gdpr-erase", headers=admin_headers)
    assert second.status_code == 200
    assert second.json()["already_erased"] is True
    # Second call must not re-stamp erased_at.
    assert second.json()["erased_at"] == first_erased_at


def test_gdpr_erase_preserves_transactions_redemptions_not_cascade_deleted(client, admin_headers):
    member_id = _create_member(client, admin_headers)
    txn_resp = client.post(
        "/api/v1/transactions",
        json={"member_id": member_id, "amount_gbp": 42.0, "channel": "pos"},
        headers=admin_headers,
    )
    assert txn_resp.status_code == 201

    client.post(f"/api/v1/members/{member_id}/gdpr-erase", headers=admin_headers)

    txns = client.get(
        "/api/v1/transactions", params={"member_id": member_id}, headers=admin_headers
    )
    assert txns.status_code == 200
    assert len(txns.json()) == 1
    assert txns.json()[0]["amount_gbp"] == 42.0


def test_gdpr_erase_requires_admin_role(client, admin_headers, member_role_headers):
    member_id = _create_member(client, admin_headers)

    resp = client.post(f"/api/v1/members/{member_id}/gdpr-erase", headers=member_role_headers)
    assert resp.status_code == 403


def test_gdpr_erase_cross_merchant_is_404(client, admin_headers):
    member_id = _create_member(client, admin_headers)

    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Other Co", "email": "other@other.example.com", "password": "pw12345"},
    )
    other_login = client.post(
        "/api/v1/auth/login", json={"email": "other@other.example.com", "password": "pw12345"}
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}

    resp = client.post(f"/api/v1/members/{member_id}/gdpr-erase", headers=other_headers)
    assert resp.status_code == 404


def test_gdpr_erase_unknown_member_is_404(client, admin_headers):
    resp = client.post("/api/v1/members/does-not-exist/gdpr-erase", headers=admin_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_gdpr_export_includes_transactions_redemptions_fraud_alerts(client, admin_headers):
    member_id = _create_member(client, admin_headers)
    client.post(
        "/api/v1/transactions",
        json={"member_id": member_id, "amount_gbp": 10.0, "channel": "pos"},
        headers=admin_headers,
    )
    reward_resp = client.post(
        "/api/v1/rewards",
        json={"name": "Free Coffee", "points_cost": 5},
        headers=admin_headers,
    )
    reward_id = reward_resp.json()["id"]
    client.post(
        "/api/v1/rewards/redeem",
        json={"member_id": member_id, "reward_id": reward_id},
        headers=admin_headers,
    )

    export_resp = client.get(f"/api/v1/members/{member_id}/gdpr-export", headers=admin_headers)
    assert export_resp.status_code == 200
    body = export_resp.json()
    assert body["member"]["id"] == member_id
    # One earn transaction from the manual ingest above, plus one redeem
    # transaction that redeem_reward() itself writes (see rewards.py) --
    # both must round-trip in the export.
    assert len(body["transactions"]) == 2
    assert len(body["redemptions"]) == 1
    assert body["fraud_alerts"] == []
    assert "exported_at" in body


def test_gdpr_export_requires_admin_role(client, admin_headers, member_role_headers):
    member_id = _create_member(client, admin_headers)

    resp = client.get(f"/api/v1/members/{member_id}/gdpr-export", headers=member_role_headers)
    assert resp.status_code == 403


def test_gdpr_export_cross_merchant_is_404(client, admin_headers):
    member_id = _create_member(client, admin_headers)

    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Other Co 2", "email": "other2@other.example.com", "password": "pw12345"},
    )
    other_login = client.post(
        "/api/v1/auth/login", json={"email": "other2@other.example.com", "password": "pw12345"}
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}

    resp = client.get(f"/api/v1/members/{member_id}/gdpr-export", headers=other_headers)
    assert resp.status_code == 404


def test_gdpr_export_after_erasure_is_410(client, admin_headers):
    member_id = _create_member(client, admin_headers)
    client.post(f"/api/v1/members/{member_id}/gdpr-erase", headers=admin_headers)

    resp = client.get(f"/api/v1/members/{member_id}/gdpr-export", headers=admin_headers)
    assert resp.status_code == 410


def _make_stale_member(db_session, merchant_id, email="stale-gdpr@example.com", days_inactive=120):
    """Guaranteed churn_risk_score == 100 ("high" band), same helper shape
    as test_winback.py -- makes the member eligible for a real win-back
    offer via a manual /winback/run call."""
    member = Member(
        merchant_id=merchant_id,
        first_name="Stale",
        last_name="Member",
        email=email,
        points_balance=250,
        tier="bronze",
        last_activity_at=datetime.now(timezone.utc) - timedelta(days=days_inactive),
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


def test_gdpr_export_includes_winback_offers_and_experiment_assignments(client, db_session, admin_headers):
    """TEST_REPORT_BATCH3.md §2 (HIGH): the export previously omitted both
    win-back offers and A/B-experiment assignments even though both tables
    hold real personal data about the member (which reward they were
    comped, at what churn score, and which experiment arm they're in).
    Grants a real win-back offer AND a real experiment assignment for the
    same member, then asserts both show up in the export with correct
    field values."""
    merchant_id = client.get("/api/v1/auth/me", headers=admin_headers).json()["merchant_id"]

    # -- Real win-back offer: rule + a churn-eligible ("stale") member + a
    # manual run, exactly the flow test_winback.py exercises. --
    winback_reward_resp = client.post(
        "/api/v1/rewards",
        json={"name": "Winback Reward", "points_cost": 150, "tier_required": "bronze"},
        headers=admin_headers,
    )
    assert winback_reward_resp.status_code == 201
    winback_reward_id = winback_reward_resp.json()["id"]

    member = _make_stale_member(db_session, merchant_id)

    rule_resp = client.put(
        "/api/v1/winback/rule",
        json={
            "enabled": True,
            "churn_risk_threshold": 65.0,
            "reward_id": winback_reward_id,
            "auto_trigger": False,
        },
        headers=admin_headers,
    )
    assert rule_resp.status_code == 200

    run_resp = client.post("/api/v1/winback/run", headers=admin_headers)
    assert run_resp.status_code == 200
    assert run_resp.json()["offers_sent"] == 1
    assert run_resp.json()["member_ids"] == [member.id]

    # -- Real A/B experiment assignment for the same member. This merchant
    # has exactly one member (created directly above, not via the API), so
    # the bulk-assignment at experiment-creation time assigns exactly them. --
    reward_a_resp = client.post(
        "/api/v1/rewards",
        json={"name": "Variant A", "points_cost": 50, "tier_required": "bronze"},
        headers=admin_headers,
    )
    reward_b_resp = client.post(
        "/api/v1/rewards",
        json={"name": "Variant B", "points_cost": 50, "tier_required": "bronze"},
        headers=admin_headers,
    )
    experiment_resp = client.post(
        "/api/v1/experiments",
        json={
            "name": "GDPR export test experiment",
            "variant_a_reward_id": reward_a_resp.json()["id"],
            "variant_b_reward_id": reward_b_resp.json()["id"],
            "traffic_split": 0.5,
        },
        headers=admin_headers,
    )
    assert experiment_resp.status_code == 201
    experiment_id = experiment_resp.json()["id"]

    assignment = (
        db_session.query(ExperimentAssignment)
        .filter(
            ExperimentAssignment.experiment_id == experiment_id,
            ExperimentAssignment.member_id == member.id,
        )
        .first()
    )
    assert assignment is not None  # sanity: the only member in this merchant must be assigned

    # -- The export must surface both. --
    export_resp = client.get(f"/api/v1/members/{member.id}/gdpr-export", headers=admin_headers)
    assert export_resp.status_code == 200
    body = export_resp.json()

    assert "winback_offers" in body
    assert len(body["winback_offers"]) == 1
    offer = body["winback_offers"][0]
    assert offer["member_id"] == member.id
    assert offer["triggered_by"] == "manual"
    assert offer["churn_risk_score_at_trigger"] >= 65.0
    assert offer["redemption_id"]

    assert "experiment_assignments" in body
    assert len(body["experiment_assignments"]) == 1
    exported_assignment = body["experiment_assignments"][0]
    assert exported_assignment["member_id"] == member.id
    assert exported_assignment["experiment_id"] == experiment_id
    assert exported_assignment["variant"] == assignment.variant
    assert exported_assignment["variant"] in ("a", "b")
