"""Notifications (PLAN_BATCH3.md §3): settings API, churn-escalation
detection -> notify, fraud-alert -> notify, cooldown/dedup, batching, and
the "no config = safe no-op" acceptance criteria.

No real Slack/SMTP network calls -- `httpx.post` is monkeypatched at the
exact call site app/services/notifications.py::send_slack uses, mirroring
test_billing.py's approach of mocking the real SDK/library call site rather
than the higher-level wrapper.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.services.notifications as notifications_module
from app.db.models import Member, Transaction, TransactionType

SLACK_URL = "https://hooks.slack.com/services/T000/B000/XXXX"


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Acme Retail", "email": "notify-owner@acme.example.com", "password": "s3cret-pw"},
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "notify-owner@acme.example.com", "password": "s3cret-pw"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def _make_stale_member(db_session, merchant_id, email="stale@example.com", days_inactive=120):
    """A member with zero recent activity and a very old last_activity_at --
    guaranteed churn_risk_score >= RISK_BAND_MEDIUM_MAX (65), i.e. "high"
    band, on the very first churn score computation (recency/frequency/
    monetary risk all saturate to 100 -> weighted score 100)."""
    member = Member(
        merchant_id=merchant_id,
        first_name="Stale",
        last_name="Member",
        email=email,
        points_balance=0,
        tier="bronze",
        last_activity_at=datetime.now(timezone.utc) - timedelta(days=days_inactive),
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


def _capture_slack(monkeypatch):
    calls: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(notifications_module.httpx, "post", fake_post)
    return calls


def _inject_fraud_transaction(db_session, merchant_id):
    """Mirrors test_fraud_detector.py::test_amount_outlier_is_flagged's
    known-working shape: 10 normal small purchases spread over the past
    month, then one wildly oversized purchase -- guaranteed to clear the
    default z-score threshold (3.0) as an amount-outlier finding."""
    now = datetime.now(timezone.utc)
    member = Member(merchant_id=merchant_id, first_name="Spike", last_name="Member", email="spike@example.com")
    db_session.add(member)
    db_session.flush()
    for i in range(10):
        db_session.add(
            Transaction(
                member_id=member.id,
                type=TransactionType.EARN.value,
                amount_gbp=20.0 + i,
                points=int(20 + i),
                created_at=now - timedelta(days=30 - i),
            )
        )
    spike = Transaction(
        member_id=member.id,
        type=TransactionType.EARN.value,
        amount_gbp=3000.0,
        points=3000,
        created_at=now - timedelta(days=1),
    )
    db_session.add(spike)
    db_session.commit()
    return member


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------


def test_get_notification_settings_defaults(client, admin_headers):
    resp = client.get("/api/v1/settings/notifications", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["notification_slack_webhook_url"] is None
    assert body["notification_email"] is None
    # NULL toggles are treated as "on" by default (wants_*_notifications).
    assert body["notify_on_churn_risk"] is True
    assert body["notify_on_fraud_alert"] is True


def test_patch_notification_settings_updates_and_supports_partial_update(client, admin_headers):
    resp = client.patch(
        "/api/v1/settings/notifications",
        json={"notification_slack_webhook_url": SLACK_URL, "notification_email": "alerts@acme.example.com"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["notification_slack_webhook_url"] == SLACK_URL
    assert body["notification_email"] == "alerts@acme.example.com"

    resp2 = client.patch(
        "/api/v1/settings/notifications", json={"notify_on_fraud_alert": False}, headers=admin_headers
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["notify_on_fraud_alert"] is False
    assert body2["notification_slack_webhook_url"] == SLACK_URL  # untouched by the partial update


def test_patch_notification_settings_requires_admin_role(client, admin_headers):
    client.post(
        "/api/v1/team/invite",
        json={"email": "notify-teammate@acme.example.com", "password": "teammate-pw1", "role": "member"},
        headers=admin_headers,
    )
    login = client.post(
        "/api/v1/auth/login", json={"email": "notify-teammate@acme.example.com", "password": "teammate-pw1"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.patch(
        "/api/v1/settings/notifications", json={"notify_on_fraud_alert": False}, headers=member_headers
    )
    assert resp.status_code == 403

    # GET remains open to any team member.
    get_resp = client.get("/api/v1/settings/notifications", headers=member_headers)
    assert get_resp.status_code == 200


# ---------------------------------------------------------------------------
# Churn escalation -> notification, cooldown/dedup (the core §3 mechanism)
# ---------------------------------------------------------------------------


def test_churn_escalation_triggers_exactly_one_slack_notification(client, db_session, admin_headers, monkeypatch):
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications", json={"notification_slack_webhook_url": SLACK_URL}, headers=admin_headers
    )
    calls = _capture_slack(monkeypatch)
    member = _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert any(r["member_id"] == member.id and r["risk_band"] == "high" for r in rows)

    assert len(calls) == 1
    assert calls[0]["url"] == SLACK_URL
    assert member.first_name in calls[0]["json"]["text"]

    db_session.refresh(member)
    assert member.last_known_risk_band == "high"
    assert member.risk_escalated_notified_at is not None


def test_churn_escalation_includes_suggested_winback_reward_when_configured(
    client, db_session, admin_headers, monkeypatch
):
    """Competitor-research-driven enrichment: the escalation alert should
    surface the merchant's saved win-back reward suggestion (same one
    GET /winback/worklist would show) inline, not just a bare name --
    without granting anything or messaging the member directly (see
    app/services/winback.py's module docstring for why the latter is out
    of scope)."""
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications", json={"notification_slack_webhook_url": SLACK_URL}, headers=admin_headers
    )
    reward_resp = client.post(
        "/api/v1/rewards",
        json={"name": "Free Coffee", "points_cost": 150, "tier_required": "bronze"},
        headers=admin_headers,
    )
    assert reward_resp.status_code == 201
    reward_id = reward_resp.json()["id"]
    rule_resp = client.put(
        "/api/v1/winback/rule",
        json={"enabled": True, "churn_risk_threshold": 65.0, "reward_id": reward_id},
        headers=admin_headers,
    )
    assert rule_resp.status_code == 200

    calls = _capture_slack(monkeypatch)
    member = _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert resp.status_code == 200

    assert len(calls) == 1
    text = calls[0]["json"]["text"]
    assert member.first_name in text
    assert "Free Coffee" in text
    assert "suggested offer" in text.lower()


def test_churn_escalation_without_winback_rule_has_plain_names(client, db_session, admin_headers, monkeypatch):
    """No rule saved -- alert stays exactly as it was before this
    enrichment, no 'suggested offer' text at all."""
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications", json={"notification_slack_webhook_url": SLACK_URL}, headers=admin_headers
    )
    calls = _capture_slack(monkeypatch)
    member = _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert resp.status_code == 200

    assert len(calls) == 1
    text = calls[0]["json"]["text"]
    assert member.first_name in text
    assert "suggested offer" not in text.lower()


def test_churn_escalation_second_request_within_cooldown_sends_nothing_more(
    client, db_session, admin_headers, monkeypatch
):
    """The actual anti-spam regression test (PLAN_BATCH3.md §3 acceptance
    criterion 3): a second recompute with no *new* escalations sends zero
    additional notifications, even though the member is still "high"."""
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications", json={"notification_slack_webhook_url": SLACK_URL}, headers=admin_headers
    )
    calls = _capture_slack(monkeypatch)
    _make_stale_member(db_session, merchant_id)

    first = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert first.status_code == 200
    assert len(calls) == 1

    second = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert second.status_code == 200
    second_row = next(r for r in second.json() if r["risk_band"] == "high")
    assert second_row  # still high band
    assert len(calls) == 1  # no second Slack POST


def test_churn_escalation_refires_after_drop_and_re_escalate(client, db_session, admin_headers, monkeypatch):
    """PLAN_BATCH3.md §3 acceptance criterion 4: proves this is escalation-
    *transition* tracking, not a one-time-ever flag -- band drops below
    high, then re-escalates in a later request, and a second notification
    fires."""
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications", json={"notification_slack_webhook_url": SLACK_URL}, headers=admin_headers
    )
    calls = _capture_slack(monkeypatch)
    member = _make_stale_member(db_session, merchant_id)

    client.get("/api/v1/ai/churn", headers=admin_headers)
    assert len(calls) == 1

    # Member re-engages: recent activity + a real purchase drops them well
    # below the "high" band.
    db_session.refresh(member)
    member.last_activity_at = datetime.now(timezone.utc)
    db_session.add(
        Transaction(member_id=member.id, type=TransactionType.EARN.value, amount_gbp=500.0, points=500)
    )
    db_session.commit()

    mid = client.get("/api/v1/ai/churn", headers=admin_headers)
    mid_row = next(r for r in mid.json() if r["member_id"] == member.id)
    assert mid_row["risk_band"] != "high"
    assert len(calls) == 1  # dropping out of high risk never notifies

    # Member lapses again.
    db_session.refresh(member)
    member.last_activity_at = datetime.now(timezone.utc) - timedelta(days=120)
    db_session.commit()

    third = client.get("/api/v1/ai/churn", headers=admin_headers)
    third_row = next(r for r in third.json() if r["member_id"] == member.id)
    assert third_row["risk_band"] == "high"
    assert len(calls) == 2  # re-escalation notifies again


def test_churn_notifications_toggle_off_suppresses_send_but_dedup_state_still_updates(
    client, db_session, admin_headers, monkeypatch
):
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications",
        json={"notification_slack_webhook_url": SLACK_URL, "notify_on_churn_risk": False},
        headers=admin_headers,
    )
    calls = _capture_slack(monkeypatch)
    member = _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert resp.status_code == 200
    assert len(calls) == 0

    db_session.refresh(member)
    assert member.last_known_risk_band == "high"  # detection/dedup state still runs


def test_no_slack_or_email_configured_is_a_safe_no_op(client, db_session, admin_headers, monkeypatch):
    merchant_id = _merchant_id(client, admin_headers)
    calls = _capture_slack(monkeypatch)
    _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert resp.status_code == 200
    assert len(calls) == 0


def test_slack_failure_never_fails_or_meaningfully_delays_the_triggering_request(
    client, db_session, admin_headers, monkeypatch
):
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications", json={"notification_slack_webhook_url": SLACK_URL}, headers=admin_headers
    )

    def broken_post(*args, **kwargs):
        raise TimeoutError("simulated Slack outage")

    monkeypatch.setattr(notifications_module.httpx, "post", broken_post)
    _make_stale_member(db_session, merchant_id)

    started = time.monotonic()
    resp = client.get("/api/v1/ai/churn", headers=admin_headers)
    elapsed = time.monotonic() - started

    assert resp.status_code == 200
    assert elapsed < 2.0


# ---------------------------------------------------------------------------
# Fraud alert -> notification, batching
# ---------------------------------------------------------------------------


def test_fraud_alert_triggers_exactly_one_batched_notification(client, db_session, admin_headers, monkeypatch):
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications", json={"notification_slack_webhook_url": SLACK_URL}, headers=admin_headers
    )
    calls = _capture_slack(monkeypatch)
    _inject_fraud_transaction(db_session, merchant_id)

    resp = client.get("/api/v1/ai/fraud-alerts?refresh=true", headers=admin_headers)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
    assert len(calls) == 1  # exactly one batched Slack POST, not one per alert

    # A second call with no *new* alerts (run_fraud_detection's own
    # existing_alert_txn_ids dedup) sends zero additional notifications.
    resp2 = client.get("/api/v1/ai/fraud-alerts?refresh=true", headers=admin_headers)
    assert resp2.status_code == 200
    assert len(calls) == 1


def test_fraud_notifications_toggle_off_suppresses_send_but_alerts_still_persist(
    client, db_session, admin_headers, monkeypatch
):
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications",
        json={"notification_slack_webhook_url": SLACK_URL, "notify_on_fraud_alert": False},
        headers=admin_headers,
    )
    calls = _capture_slack(monkeypatch)
    _inject_fraud_transaction(db_session, merchant_id)

    resp = client.get("/api/v1/ai/fraud-alerts?refresh=true", headers=admin_headers)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1  # fraud detection still runs/persists alerts
    assert len(calls) == 0  # but no notification fires
