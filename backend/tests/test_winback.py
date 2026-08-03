"""Win-back worklist: reward-preference CRUD + the computed, read-only
worklist. Reworked from an auto-executing campaign feature (grant-for-free
Redemption, anti-repeat-offer guard, auto_trigger) into a pure read: no
Redemption or WinbackOffer row is ever written by this feature anymore.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Member, Redemption, WinbackOffer


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Acme Retail", "email": "winback-owner@acme.example.com", "password": "s3cret-pw"},
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "winback-owner@acme.example.com", "password": "s3cret-pw"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def _make_stale_member(db_session, merchant_id, email="stale@example.com", days_inactive=120):
    """Guaranteed churn_risk_score == 100 ("high" band) -- see
    test_notifications.py's identical helper for the RFM math."""
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


def _create_reward(client, headers, name="Free Coffee", points_cost=150) -> str:
    resp = client.post(
        "/api/v1/rewards",
        json={"name": name, "points_cost": points_cost, "tier_required": "bronze"},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _put_rule(client, headers, reward_id, enabled=True, threshold=65.0):
    resp = client.put(
        "/api/v1/winback/rule",
        json={"enabled": enabled, "churn_risk_threshold": threshold, "reward_id": reward_id},
        headers=headers,
    )
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Rule CRUD
# ---------------------------------------------------------------------------


def test_get_rule_default_shape_when_none_saved_yet(client, admin_headers):
    resp = client.get("/api/v1/winback/rule", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] is None
    assert body["enabled"] is False
    assert body["churn_risk_threshold"] == 65.0
    assert body["reward_id"] is None
    assert "auto_trigger" not in body


def test_put_rule_creates_then_upserts(client, admin_headers):
    reward_id = _create_reward(client, admin_headers)
    created = _put_rule(client, admin_headers, reward_id, enabled=True, threshold=70.0)
    assert created["enabled"] is True
    assert created["churn_risk_threshold"] == 70.0
    assert created["reward_id"] == reward_id
    rule_id = created["id"]

    updated = _put_rule(client, admin_headers, reward_id, enabled=False, threshold=80.0)
    assert updated["id"] == rule_id  # same row, upserted not duplicated
    assert updated["enabled"] is False
    assert updated["churn_risk_threshold"] == 80.0


def test_put_rule_rejects_reward_not_belonging_to_merchant_or_inactive(client, admin_headers):
    resp = client.put(
        "/api/v1/winback/rule",
        json={"enabled": True, "churn_risk_threshold": 65.0, "reward_id": "does-not-exist"},
        headers=admin_headers,
    )
    assert resp.status_code == 400


def test_rule_write_requires_admin_role_but_reads_stay_open(client, admin_headers):
    reward_id = _create_reward(client, admin_headers)
    client.post(
        "/api/v1/team/invite",
        json={"email": "winback-teammate@acme.example.com", "password": "teammate-pw1", "role": "member"},
        headers=admin_headers,
    )
    login = client.post(
        "/api/v1/auth/login", json={"email": "winback-teammate@acme.example.com", "password": "teammate-pw1"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    put_resp = client.put(
        "/api/v1/winback/rule",
        json={"enabled": True, "churn_risk_threshold": 65.0, "reward_id": reward_id},
        headers=member_headers,
    )
    assert put_resp.status_code == 403

    # GET rule/worklist stay open to any team member.
    assert client.get("/api/v1/winback/rule", headers=member_headers).status_code == 200
    assert client.get("/api/v1/winback/worklist", headers=member_headers).status_code == 200


# ---------------------------------------------------------------------------
# Worklist: read-only, no execution, no persistence
# ---------------------------------------------------------------------------


def test_worklist_surfaces_at_risk_member_with_suggested_reward(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers, name="Free Coffee", points_cost=25)
    _put_rule(client, admin_headers, reward_id, enabled=True, threshold=65.0)
    member = _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/winback/worklist", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["member_id"] == member.id
    assert body[0]["churn_risk_score"] >= 65.0
    assert body[0]["suggested_reward_id"] == reward_id
    assert body[0]["suggested_reward_name"] == "Free Coffee"


def test_worklist_is_idempotent_and_never_writes_anything(client, db_session, admin_headers):
    """Calling the worklist repeatedly must not create Redemption or
    WinbackOffer rows -- this is the core guarantee of the rework: no
    execution, ever, from this feature."""
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers)
    _put_rule(client, admin_headers, reward_id, enabled=True, threshold=65.0)
    member = _make_stale_member(db_session, merchant_id)
    starting_balance = member.points_balance

    first = client.get("/api/v1/winback/worklist", headers=admin_headers).json()
    second = client.get("/api/v1/winback/worklist", headers=admin_headers).json()
    assert len(first) == 1
    assert len(second) == 1  # member still surfaces -- nothing "consumed" the suggestion

    assert db_session.query(WinbackOffer).count() == 0
    assert db_session.query(Redemption).filter(Redemption.source == "winback").count() == 0

    db_session.refresh(member)
    assert member.points_balance == starting_balance


def test_worklist_without_saved_rule_uses_default_threshold_no_suggested_reward(
    client, db_session, admin_headers
):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/winback/worklist", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["member_id"] == member.id
    assert body[0]["suggested_reward_id"] is None
    assert body[0]["suggested_reward_name"] is None


def test_worklist_excludes_members_below_threshold(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers)
    # Threshold above what any member can reach (score is capped at 100).
    _put_rule(client, admin_headers, reward_id, enabled=True, threshold=101.0)
    _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/winback/worklist", headers=admin_headers)
    assert resp.json() == []


def test_worklist_sorted_highest_risk_first(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers)
    _put_rule(client, admin_headers, reward_id, enabled=True, threshold=10.0)
    less_stale = _make_stale_member(db_session, merchant_id, email="less-stale@example.com", days_inactive=95)
    more_stale = _make_stale_member(db_session, merchant_id, email="more-stale@example.com", days_inactive=150)

    body = client.get("/api/v1/winback/worklist", headers=admin_headers).json()
    ids_in_order = [row["member_id"] for row in body]
    assert set(ids_in_order) == {less_stale.id, more_stale.id}
    scores = {row["member_id"]: row["churn_risk_score"] for row in body}
    assert scores[ids_in_order[0]] >= scores[ids_in_order[1]]


def test_removed_execution_endpoints_are_gone(client, admin_headers):
    """The old auto-executing endpoints must not exist post-rework."""
    assert client.post("/api/v1/winback/run", headers=admin_headers).status_code == 404
    assert client.get("/api/v1/winback/offers", headers=admin_headers).status_code == 404
