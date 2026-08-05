"""Actionable audience exports (competitive-brief backlog item #4) --
see app/services/audience_export.py for the Mailchimp/Klaviyo CSV shape
and why WhatsApp/phone-based export isn't included.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Member, Transaction, TransactionType, UsageEvent


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Export Test Co", "email": "export-owner@acme.example.com", "password": "s3cret-pw"},
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "export-owner@acme.example.com", "password": "s3cret-pw"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def _make_member(db_session, merchant_id, email, days_inactive, first_name="Test", last_name="Member"):
    member = Member(
        merchant_id=merchant_id,
        first_name=first_name,
        last_name=last_name,
        email=email,
        points_balance=0,
        tier="bronze",
        last_activity_at=datetime.now(timezone.utc) - timedelta(days=days_inactive),
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


def _rows(csv_text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(csv_text)))


def test_export_all_members_generic_format(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    _make_member(db_session, merchant_id, "stale@example.com", days_inactive=120)
    _make_member(db_session, merchant_id, "fresh@example.com", days_inactive=1)

    resp = client.get("/api/v1/members/export.csv", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")

    rows = _rows(resp.text)
    assert rows[0] == ["email", "first_name", "last_name", "tags"]
    emails = {row[0] for row in rows[1:]}
    assert emails == {"stale@example.com", "fresh@example.com"}


def test_export_filters_by_risk_band(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    _make_member(db_session, merchant_id, "stale@example.com", days_inactive=120)
    _make_member(db_session, merchant_id, "fresh@example.com", days_inactive=1)

    resp = client.get("/api/v1/members/export.csv?risk=high", headers=admin_headers)
    rows = _rows(resp.text)
    emails = {row[0] for row in rows[1:]}
    assert emails == {"stale@example.com"}
    assert "ledgerly-high-risk" in rows[1][3]


def test_export_mailchimp_and_klaviyo_headers(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    _make_member(db_session, merchant_id, "member@example.com", days_inactive=1)

    mailchimp_rows = _rows(client.get("/api/v1/members/export.csv?format=mailchimp", headers=admin_headers).text)
    assert mailchimp_rows[0] == ["Email Address", "First Name", "Last Name", "Tags"]

    klaviyo_rows = _rows(client.get("/api/v1/members/export.csv?format=klaviyo", headers=admin_headers).text)
    assert klaviyo_rows[0] == ["Email", "First Name", "Last Name", "Tags"]


def test_export_rejects_invalid_risk_and_format(client, admin_headers):
    resp1 = client.get("/api/v1/members/export.csv?risk=extreme", headers=admin_headers)
    assert resp1.status_code == 400

    resp2 = client.get("/api/v1/members/export.csv?format=hubspot", headers=admin_headers)
    assert resp2.status_code == 400


def test_export_excludes_erased_members(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    member = _make_member(db_session, merchant_id, "toerase@example.com", days_inactive=1)

    erase_resp = client.post(f"/api/v1/members/{member.id}/gdpr-erase", headers=admin_headers)
    assert erase_resp.status_code == 200

    resp = client.get("/api/v1/members/export.csv", headers=admin_headers)
    rows = _rows(resp.text)
    emails = {row[0] for row in rows[1:]}
    assert "toerase@example.com" not in emails
    assert not any("deleted.ledgerly.invalid" in email for email in emails)


def test_export_records_usage_event(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    _make_member(db_session, merchant_id, "member@example.com", days_inactive=1)

    client.get("/api/v1/members/export.csv", headers=admin_headers)

    events = db_session.query(UsageEvent).filter(UsageEvent.merchant_id == merchant_id).all()
    assert any(e.kind == "audience_export" for e in events)


def test_export_does_not_shadow_member_detail_route(client, db_session, admin_headers):
    """Regression guard: GET /members/export.csv must resolve to the
    export endpoint, not be captured by GET /members/{member_id}."""
    merchant_id = _merchant_id(client, admin_headers)
    _make_member(db_session, merchant_id, "member@example.com", days_inactive=1)

    resp = client.get("/api/v1/members/export.csv", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
