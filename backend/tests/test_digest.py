"""Weekly digest (competitive-brief backlog item #2): preview/send
endpoints, the opt-in settings toggle, and the auto-send-via-GET-/ai/churn
piggyback (app/services/digest.py's module docstring explains why there's
no scheduler and how this hooks in instead).

Mirrors test_notifications.py's approach: no real Slack/SMTP network
calls -- httpx.post is monkeypatched at the exact call site
app/services/notifications.py::send_slack uses.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.db.models import Member, Merchant, UsageEvent

SLACK_URL = "https://hooks.slack.com/services/T111/B111/YYYY"


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Digest Test Co", "email": "digest-owner@acme.example.com", "password": "s3cret-pw"},
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "digest-owner@acme.example.com", "password": "s3cret-pw"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def _make_stale_member(db_session, merchant_id, email="stale-digest@example.com", days_inactive=120):
    member = Member(
        merchant_id=merchant_id,
        first_name="Stale",
        last_name="AtRisk",
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

    import app.services.notifications as notifications_module

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(notifications_module.httpx, "post", fake_post)
    return calls


# ---------------------------------------------------------------------------
# Settings toggle
# ---------------------------------------------------------------------------


def test_weekly_digest_toggle_defaults_off(client, admin_headers):
    resp = client.get("/api/v1/settings/notifications", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["notify_weekly_digest"] is False


def test_weekly_digest_toggle_can_be_enabled(client, admin_headers):
    resp = client.patch(
        "/api/v1/settings/notifications", json={"notify_weekly_digest": True}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["notify_weekly_digest"] is True

    get_resp = client.get("/api/v1/settings/notifications", headers=admin_headers)
    assert get_resp.json()["notify_weekly_digest"] is True


# ---------------------------------------------------------------------------
# Preview / status
# ---------------------------------------------------------------------------


def test_preview_digest_with_no_members_is_a_quiet_week(client, admin_headers):
    resp = client.get("/api/v1/digest/preview", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_members"] == 0
    assert body["at_risk_count"] == 0
    assert body["at_risk_members"] == []
    assert "quiet week" in body["headline"]


def test_preview_digest_surfaces_high_risk_members(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/digest/preview", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_members"] == 1
    assert body["at_risk_count"] == 1
    assert any(m["member_id"] == member.id for m in body["at_risk_members"])


def test_digest_status_reflects_toggle_and_channel(client, admin_headers):
    resp = client.get("/api/v1/digest/status", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["has_notification_channel"] is False
    assert body["last_digest_sent_at"] is None

    client.patch(
        "/api/v1/settings/notifications",
        json={"notify_weekly_digest": True, "notification_slack_webhook_url": SLACK_URL},
        headers=admin_headers,
    )
    resp2 = client.get("/api/v1/digest/status", headers=admin_headers)
    body2 = resp2.json()
    assert body2["enabled"] is True
    assert body2["has_notification_channel"] is True


# ---------------------------------------------------------------------------
# On-demand send
# ---------------------------------------------------------------------------


def test_send_digest_delivers_via_slack_and_records_usage(client, db_session, admin_headers, monkeypatch):
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications", json={"notification_slack_webhook_url": SLACK_URL}, headers=admin_headers
    )
    calls = _capture_slack(monkeypatch)
    _make_stale_member(db_session, merchant_id)

    resp = client.post("/api/v1/digest/send", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sent_via"] == ["slack"]
    assert body["last_digest_sent_at"] is not None

    assert len(calls) == 1
    assert calls[0]["url"] == SLACK_URL

    events = db_session.query(UsageEvent).filter(UsageEvent.merchant_id == merchant_id).all()
    assert any(e.kind == "weekly_digest" for e in events)

    merchant = db_session.get(Merchant, merchant_id)
    assert merchant.last_digest_sent_at is not None


def test_send_digest_with_no_channel_configured_still_computes_but_sends_nowhere(client, admin_headers):
    resp = client.post("/api/v1/digest/send", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["sent_via"] == []


def test_send_digest_requires_admin_role(client, admin_headers):
    client.post(
        "/api/v1/team/invite",
        json={"email": "digest-teammate@acme.example.com", "password": "teammate-pw1", "role": "member"},
        headers=admin_headers,
    )
    login = client.post(
        "/api/v1/auth/login", json={"email": "digest-teammate@acme.example.com", "password": "teammate-pw1"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post("/api/v1/digest/send", headers=member_headers)
    assert resp.status_code == 403

    # Preview/status remain open to any team member.
    assert client.get("/api/v1/digest/preview", headers=member_headers).status_code == 200
    assert client.get("/api/v1/digest/status", headers=member_headers).status_code == 200


# ---------------------------------------------------------------------------
# Auto-send piggybacking on GET /ai/churn
# ---------------------------------------------------------------------------


def test_auto_send_fires_on_churn_endpoint_when_enabled_and_due(client, db_session, admin_headers, monkeypatch):
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications",
        json={"notify_weekly_digest": True, "notification_slack_webhook_url": SLACK_URL},
        headers=admin_headers,
    )
    calls = _capture_slack(monkeypatch)
    _make_stale_member(db_session, merchant_id)

    resp = client.get("/api/v1/ai/churn", headers=admin_headers)
    assert resp.status_code == 200

    # One call for the churn-escalation notification, one for the digest.
    digest_calls = [c for c in calls if "digest" in c["json"]["text"].lower()]
    assert len(digest_calls) == 1

    merchant = db_session.get(Merchant, merchant_id)
    assert merchant.last_digest_sent_at is not None

    events = db_session.query(UsageEvent).filter(UsageEvent.merchant_id == merchant_id).all()
    assert any(e.kind == "weekly_digest" for e in events)


def test_auto_send_does_not_refire_before_next_interval(client, db_session, admin_headers, monkeypatch):
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications",
        json={"notify_weekly_digest": True, "notification_slack_webhook_url": SLACK_URL},
        headers=admin_headers,
    )
    calls = _capture_slack(monkeypatch)
    _make_stale_member(db_session, merchant_id)

    client.get("/api/v1/ai/churn", headers=admin_headers)
    first_digest_calls = [c for c in calls if "digest" in c["json"]["text"].lower()]
    assert len(first_digest_calls) == 1

    calls.clear()
    client.get("/api/v1/ai/churn", headers=admin_headers)
    second_digest_calls = [c for c in calls if "digest" in c["json"]["text"].lower()]
    assert len(second_digest_calls) == 0


def test_auto_send_does_not_fire_when_toggle_is_off(client, db_session, admin_headers, monkeypatch):
    merchant_id = _merchant_id(client, admin_headers)
    client.patch(
        "/api/v1/settings/notifications", json={"notification_slack_webhook_url": SLACK_URL}, headers=admin_headers
    )
    calls = _capture_slack(monkeypatch)
    _make_stale_member(db_session, merchant_id)

    client.get("/api/v1/ai/churn", headers=admin_headers)
    digest_calls = [c for c in calls if "digest" in c["json"]["text"].lower()]
    assert len(digest_calls) == 0

    merchant = db_session.get(Merchant, merchant_id)
    assert merchant.last_digest_sent_at is None
