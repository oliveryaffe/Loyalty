"""Manual, merchant-triggered win-back email (app/services/winback.py::
send_winback_email + POST /members/{id}/winback-email). Reuses
test_notifications.py's approach of monkeypatching the exact call site
(smtplib.SMTP) rather than the higher-level wrapper -- no real network
calls. `settings.smtp_host` defaults to None/unset in every other test in
this suite (nothing else touches it), so each test here explicitly turns
it "on" via monkeypatch rather than relying on env config.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.services.notifications as notifications_module
from app.db.models import Member

SMTP_HOST = "smtp.test.example.com"


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={
            "business_name": "Winback Coffee Co",
            "email": "winback-email-owner@acme.example.com",
            "password": "s3cret-pw",
        },
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "winback-email-owner@acme.example.com", "password": "s3cret-pw"},
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def _make_member(db_session, merchant_id, email="atrisk@example.com") -> Member:
    member = Member(
        merchant_id=merchant_id,
        first_name="Jamie",
        last_name="Regular",
        email=email,
        points_balance=0,
        tier="bronze",
        last_activity_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


def _create_reward(client, headers, name="Free Coffee") -> str:
    resp = client.post(
        "/api/v1/rewards",
        json={"name": name, "points_cost": 150, "tier_required": "bronze"},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _put_rule(client, headers, reward_id, threshold=65.0):
    resp = client.put(
        "/api/v1/winback/rule",
        json={"enabled": True, "churn_risk_threshold": threshold, "reward_id": reward_id},
        headers=headers,
    )
    assert resp.status_code == 200


class _FakeSMTP:
    """Records every message sent via `send_message`, no real connection.
    Used as `smtplib.SMTP(...)` -- a context manager, same shape as the
    real class."""

    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, username, password):
        pass

    def send_message(self, message):
        _FakeSMTP.sent.append(message)


@pytest.fixture()
def smtp_enabled(monkeypatch):
    """Turns SMTP "on" (settings.smtp_host set) and replaces smtplib.SMTP
    with the in-memory fake above. Returns the fake's message list."""
    monkeypatch.setattr(notifications_module.settings, "smtp_host", SMTP_HOST)
    monkeypatch.setattr(notifications_module.settings, "smtp_from_address", "notifications@ledgerly.app")
    _FakeSMTP.sent = []
    monkeypatch.setattr(notifications_module.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP.sent


def test_sends_and_mentions_suggested_reward_when_configured(client, db_session, admin_headers, smtp_enabled):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_member(db_session, merchant_id)
    reward_id = _create_reward(client, admin_headers, name="Free Coffee")
    _put_rule(client, admin_headers, reward_id)

    resp = client.post(f"/api/v1/members/{member.id}/winback-email", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] is True
    assert body["reason"] == "sent"

    assert len(smtp_enabled) == 1
    sent_message = smtp_enabled[0]
    assert sent_message["To"] == member.email
    assert "Free Coffee" in sent_message.get_content()
    assert "Winback Coffee Co" in sent_message["From"]


def test_sends_plain_message_without_reward_when_no_rule_saved(client, db_session, admin_headers, smtp_enabled):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_member(db_session, merchant_id)

    resp = client.post(f"/api/v1/members/{member.id}/winback-email", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["sent"] is True

    sent_message = smtp_enabled[0]
    content = sent_message.get_content()
    assert "on us" not in content  # no reward line without a saved rule


def test_reply_to_set_to_merchant_notification_email(client, db_session, admin_headers, smtp_enabled):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_member(db_session, merchant_id)
    resp = client.patch(
        "/api/v1/settings/notifications",
        json={"notification_email": "owner-inbox@winbackcoffee.example.com"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    client.post(f"/api/v1/members/{member.id}/winback-email", headers=admin_headers)

    sent_message = smtp_enabled[0]
    assert sent_message["Reply-To"] == "owner-inbox@winbackcoffee.example.com"


def test_smtp_not_configured_is_a_safe_no_send(client, db_session, admin_headers):
    """No smtp_enabled fixture here -- settings.smtp_host stays at its
    default (unset), same as every other test in this suite."""
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_member(db_session, merchant_id)

    resp = client.post(f"/api/v1/members/{member.id}/winback-email", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] is False
    assert body["reason"] == "smtp_not_configured"

    db_session.refresh(member)
    assert member.last_winback_email_sent_at is None


def test_cooldown_blocks_a_second_send_and_does_not_resend(client, db_session, admin_headers, smtp_enabled):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_member(db_session, merchant_id)

    first = client.post(f"/api/v1/members/{member.id}/winback-email", headers=admin_headers)
    assert first.json()["sent"] is True
    assert len(smtp_enabled) == 1

    second = client.post(f"/api/v1/members/{member.id}/winback-email", headers=admin_headers)
    body = second.json()
    assert body["sent"] is False
    assert body["reason"] == "cooldown"
    assert body["cooldown_until"] is not None
    # Still only the one message from the first send -- no duplicate.
    assert len(smtp_enabled) == 1


def test_erased_member_returns_404(client, db_session, admin_headers, smtp_enabled):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_member(db_session, merchant_id)
    erase_resp = client.post(f"/api/v1/members/{member.id}/gdpr-erase", headers=admin_headers)
    assert erase_resp.status_code == 200

    resp = client.post(f"/api/v1/members/{member.id}/winback-email", headers=admin_headers)
    assert resp.status_code == 404
    assert len(smtp_enabled) == 0


def test_member_from_another_merchant_returns_404(client, db_session, admin_headers, smtp_enabled):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Other Shop", "email": "other-owner@acme.example.com", "password": "s3cret-pw"},
    )
    other_login = client.post(
        "/api/v1/auth/login", json={"email": "other-owner@acme.example.com", "password": "s3cret-pw"}
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}
    other_merchant_id = _merchant_id(client, other_headers)
    other_member = _make_member(db_session, other_merchant_id, email="notyours@example.com")

    resp = client.post(f"/api/v1/members/{other_member.id}/winback-email", headers=admin_headers)
    assert resp.status_code == 404
    assert len(smtp_enabled) == 0
