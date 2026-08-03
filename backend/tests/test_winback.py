"""Win-back campaigns (PLAN_BATCH3.md §4): rule CRUD, manual trigger +
grant-for-free semantics, the anti-repeat-offer guard (both the
eligibility-query level and the DB unique-constraint level), the
`auto_trigger=False`-by-default safety rail, and the auto-trigger path's
explicit dependency on §3's escalation detection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import Member, Redemption, RedemptionStatus, WinbackOffer, WinbackRule


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


def _put_rule(client, headers, reward_id, enabled=True, threshold=65.0, auto_trigger=False):
    resp = client.put(
        "/api/v1/winback/rule",
        json={
            "enabled": enabled,
            "churn_risk_threshold": threshold,
            "reward_id": reward_id,
            "auto_trigger": auto_trigger,
        },
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
    assert body["auto_trigger"] is False


def test_put_rule_creates_then_upserts(client, admin_headers):
    reward_id = _create_reward(client, admin_headers)
    created = _put_rule(client, admin_headers, reward_id, enabled=True, threshold=70.0, auto_trigger=False)
    assert created["enabled"] is True
    assert created["churn_risk_threshold"] == 70.0
    assert created["reward_id"] == reward_id
    rule_id = created["id"]

    updated = _put_rule(client, admin_headers, reward_id, enabled=False, threshold=80.0, auto_trigger=True)
    assert updated["id"] == rule_id  # same row, upserted not duplicated
    assert updated["enabled"] is False
    assert updated["churn_risk_threshold"] == 80.0
    assert updated["auto_trigger"] is True


def test_put_rule_rejects_reward_not_belonging_to_merchant_or_inactive(client, admin_headers):
    resp = client.put(
        "/api/v1/winback/rule",
        json={"enabled": True, "churn_risk_threshold": 65.0, "reward_id": "does-not-exist", "auto_trigger": False},
        headers=admin_headers,
    )
    assert resp.status_code == 400


def test_rule_endpoints_require_admin_role(client, admin_headers):
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
        json={"enabled": True, "churn_risk_threshold": 65.0, "reward_id": reward_id, "auto_trigger": False},
        headers=member_headers,
    )
    assert put_resp.status_code == 403

    run_resp = client.post("/api/v1/winback/run", headers=member_headers)
    assert run_resp.status_code == 403

    # GET rule/offers stay open to any team member.
    assert client.get("/api/v1/winback/rule", headers=member_headers).status_code == 200
    assert client.get("/api/v1/winback/offers", headers=member_headers).status_code == 200


# ---------------------------------------------------------------------------
# Manual trigger: grant-for-free semantics + anti-repeat-offer guard
# ---------------------------------------------------------------------------


def test_manual_run_offers_eligible_member_a_free_comped_reward(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers, points_cost=500)
    _put_rule(client, admin_headers, reward_id, enabled=True, threshold=65.0)
    member = _make_stale_member(db_session, merchant_id)
    starting_balance = member.points_balance

    resp = client.post("/api/v1/winback/run", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["offers_sent"] == 1
    assert body["member_ids"] == [member.id]

    offer = db_session.query(WinbackOffer).filter(WinbackOffer.member_id == member.id).first()
    assert offer is not None
    assert offer.triggered_by == "manual"
    assert offer.churn_risk_score_at_trigger >= 65.0

    redemption = db_session.get(Redemption, offer.redemption_id)
    assert redemption is not None
    assert redemption.source == "winback"
    assert redemption.points_spent == 0
    assert redemption.status == RedemptionStatus.COMPLETED.value
    assert redemption.transaction_id is None  # no Transaction row -- see grant_winback_reward's docstring

    # Regression check (same shape as Batch 2's mint_points=false criterion):
    # the comped reward must NOT touch the member's points balance.
    db_session.refresh(member)
    assert member.points_balance == starting_balance


def test_manual_run_second_call_sends_zero_offers(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers)
    _put_rule(client, admin_headers, reward_id, enabled=True, threshold=65.0)
    _make_stale_member(db_session, merchant_id)

    first = client.post("/api/v1/winback/run", headers=admin_headers)
    assert first.json()["offers_sent"] == 1

    second = client.post("/api/v1/winback/run", headers=admin_headers)
    assert second.json() == {"offers_sent": 0, "member_ids": []}


def test_winback_offer_unique_constraint_rejects_duplicate_member_id(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers)
    _put_rule(client, admin_headers, reward_id, enabled=True, threshold=65.0)
    member = _make_stale_member(db_session, merchant_id)

    run_resp = client.post("/api/v1/winback/run", headers=admin_headers)
    assert run_resp.json()["offers_sent"] == 1

    rule = db_session.query(WinbackRule).filter(WinbackRule.merchant_id == merchant_id).first()

    # Directly attempt to violate the unique constraint -- the DB rejection
    # itself is the enforced guarantee, not just the eligibility query.
    dup_redemption = Redemption(
        member_id=member.id,
        reward_id=reward_id,
        points_spent=0,
        status=RedemptionStatus.COMPLETED.value,
        source="winback",
    )
    db_session.add(dup_redemption)
    db_session.flush()
    dup_offer = WinbackOffer(
        merchant_id=merchant_id,
        member_id=member.id,
        rule_id=rule.id,
        redemption_id=dup_redemption.id,
        churn_risk_score_at_trigger=100.0,
        triggered_by="manual",
    )
    db_session.add(dup_offer)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_manual_run_returns_zero_when_rule_disabled(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers)
    _put_rule(client, admin_headers, reward_id, enabled=False, threshold=65.0)
    _make_stale_member(db_session, merchant_id)

    resp = client.post("/api/v1/winback/run", headers=admin_headers)
    assert resp.json() == {"offers_sent": 0, "member_ids": []}


def test_manual_run_skips_members_below_threshold(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers)
    # Threshold above what any member can reach (score is capped at 100).
    _put_rule(client, admin_headers, reward_id, enabled=True, threshold=101.0)
    _make_stale_member(db_session, merchant_id)

    resp = client.post("/api/v1/winback/run", headers=admin_headers)
    assert resp.json() == {"offers_sent": 0, "member_ids": []}


def test_winback_offers_history_endpoint(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers)
    _put_rule(client, admin_headers, reward_id, enabled=True, threshold=65.0)
    member = _make_stale_member(db_session, merchant_id)

    client.post("/api/v1/winback/run", headers=admin_headers)

    offers = client.get("/api/v1/winback/offers", headers=admin_headers).json()
    assert len(offers) == 1
    assert offers[0]["member_id"] == member.id
    assert offers[0]["triggered_by"] == "manual"


# ---------------------------------------------------------------------------
# Auto-trigger: depends on §3's escalation detection, off by default
# ---------------------------------------------------------------------------


def test_auto_trigger_defaults_to_false_no_offer_on_escalation_without_manual_call(
    client, db_session, admin_headers
):
    """PLAN_BATCH3.md §4 acceptance criterion 3 (auto_trigger=false half):
    a fresh escalation to "high" above threshold produces zero offers via
    GET /ai/churn alone -- only an explicit POST /winback/run sends any."""
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers)
    _put_rule(client, admin_headers, reward_id, enabled=True, threshold=65.0, auto_trigger=False)
    _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert resp.status_code == 200

    offers = client.get("/api/v1/winback/offers", headers=admin_headers).json()
    assert offers == []

    # The manual path still works on demand.
    run_resp = client.post("/api/v1/winback/run", headers=admin_headers)
    assert run_resp.json()["offers_sent"] == 1


def test_auto_trigger_true_grants_offer_on_escalation_with_no_manual_call(client, db_session, admin_headers):
    """PLAN_BATCH3.md §4 acceptance criterion 3 (auto_trigger=true half):
    depends directly on §3's check_churn_escalations -- a GET /ai/churn call
    that surfaces a fresh escalation above threshold produces a
    WinbackOffer(triggered_by="auto") with no manual call needed."""
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers)
    _put_rule(client, admin_headers, reward_id, enabled=True, threshold=65.0, auto_trigger=True)
    member = _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert resp.status_code == 200

    offers = client.get("/api/v1/winback/offers", headers=admin_headers).json()
    assert len(offers) == 1
    assert offers[0]["member_id"] == member.id
    assert offers[0]["triggered_by"] == "auto"

    # A second churn recompute (no new escalation, cooldown still active)
    # must not create a second offer for the same member.
    second = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert second.status_code == 200
    offers_after = client.get("/api/v1/winback/offers", headers=admin_headers).json()
    assert len(offers_after) == 1


def test_auto_trigger_true_but_rule_disabled_still_sends_nothing(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_id = _create_reward(client, admin_headers)
    _put_rule(client, admin_headers, reward_id, enabled=False, threshold=65.0, auto_trigger=True)
    _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert resp.status_code == 200

    offers = client.get("/api/v1/winback/offers", headers=admin_headers).json()
    assert offers == []
