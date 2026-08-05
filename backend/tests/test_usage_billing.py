"""Usage-based pricing (app/services/usage.py): UsageEvent recording at
the two billable insight-run call sites (CSV upload, report.csv export),
GET /billing/plans, and GET /billing/usage. Replaces the earlier
per-member-count tier caps -- see usage.py's module docstring for why.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Merchant, UsageEvent
from app.services.usage import PLAN_DEFINITIONS, TRIAL_PLAN, compute_usage_summary, current_period_start

SAMPLE_CSV = (
    "customer_email,transaction_date,amount_gbp\n"
    "usage1@example.com,2026-05-01,25.00\n"
    "usage2@example.com,2026-05-02,18.50\n"
)


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Acme Retail", "email": "usage-owner@acme.example.com", "password": "s3cret-pw"},
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "usage-owner@acme.example.com", "password": "s3cret-pw"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def _upload(client, headers, content: bytes = SAMPLE_CSV.encode()):
    return client.post(
        "/api/v1/insights/upload",
        headers=headers,
        files={"file": ("upload.csv", io.BytesIO(content), "text/csv")},
    )


# ---------------------------------------------------------------------------
# GET /billing/plans
# ---------------------------------------------------------------------------


def test_plans_list_has_no_member_count_language(client, admin_headers):
    resp = client.get("/api/v1/billing/plans", headers=admin_headers)
    assert resp.status_code == 200
    plans = resp.json()
    tiers = {p["tier"] for p in plans}
    assert tiers == {"starter", "growth", "scale"}
    for plan in plans:
        assert plan["included_runs"] > 0
        assert plan["base_price_gbp"] > 0
        assert plan["overage_price_gbp"] > 0


def test_plans_match_backend_definitions(client, admin_headers):
    resp = client.get("/api/v1/billing/plans", headers=admin_headers)
    by_tier = {p["tier"]: p for p in resp.json()}
    for tier, definition in PLAN_DEFINITIONS.items():
        assert by_tier[tier]["included_runs"] == definition.included_runs
        assert by_tier[tier]["base_price_gbp"] == definition.base_price_gbp
        assert by_tier[tier]["overage_price_gbp"] == definition.overage_price_gbp


# ---------------------------------------------------------------------------
# GET /billing/usage
# ---------------------------------------------------------------------------


def test_usage_starts_at_zero_for_new_merchant(client, admin_headers):
    resp = client.get("/api/v1/billing/usage", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["insight_runs_used"] == 0
    assert body["overage_runs"] == 0
    assert body["estimated_overage_cost_gbp"] == 0


def test_usage_defaults_to_trial_plan_when_no_tier_subscribed(client, admin_headers):
    resp = client.get("/api/v1/billing/usage", headers=admin_headers)
    body = resp.json()
    assert body["tier"] == TRIAL_PLAN.tier
    assert body["included_runs"] == TRIAL_PLAN.included_runs


def test_csv_upload_records_one_insight_run(client, admin_headers):
    upload_resp = _upload(client, admin_headers)
    assert upload_resp.status_code == 200

    usage_resp = client.get("/api/v1/billing/usage", headers=admin_headers)
    assert usage_resp.json()["insight_runs_used"] == 1


def test_report_download_records_one_insight_run(client, admin_headers):
    report_resp = client.get("/api/v1/insights/report.csv", headers=admin_headers)
    assert report_resp.status_code == 200

    usage_resp = client.get("/api/v1/billing/usage", headers=admin_headers)
    assert usage_resp.json()["insight_runs_used"] == 1


def test_multiple_actions_accumulate_usage(client, admin_headers):
    _upload(client, admin_headers)
    client.get("/api/v1/insights/report.csv", headers=admin_headers)
    client.get("/api/v1/insights/report.csv", headers=admin_headers)

    usage_resp = client.get("/api/v1/billing/usage", headers=admin_headers)
    assert usage_resp.json()["insight_runs_used"] == 3


def test_dashboard_reads_do_not_count_as_usage(client, admin_headers):
    """Viewing future-value / next-best-product is incidental to having
    the dashboard open, not a deliberate "process my data" action -- must
    never be billed."""
    client.get("/api/v1/insights/future-value", headers=admin_headers)
    client.get("/api/v1/members", headers=admin_headers)

    usage_resp = client.get("/api/v1/billing/usage", headers=admin_headers)
    assert usage_resp.json()["insight_runs_used"] == 0


def test_usage_scoped_per_merchant(client, admin_headers):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Other Biz", "email": "other-usage@acme.example.com", "password": "s3cret-pw"},
    )
    other_login = client.post(
        "/api/v1/auth/login", json={"email": "other-usage@acme.example.com", "password": "s3cret-pw"}
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}

    _upload(client, admin_headers)

    other_usage = client.get("/api/v1/billing/usage", headers=other_headers)
    assert other_usage.json()["insight_runs_used"] == 0


def test_usage_requires_auth(client):
    resp = client.get("/api/v1/billing/usage")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Overage calculation (service-level, exercising the boundary directly --
# generating hundreds of real HTTP uploads to cross an included-runs
# threshold would be slow and wouldn't test anything the unit-level
# arithmetic doesn't already cover)
# ---------------------------------------------------------------------------


def test_overage_computed_once_over_included_allowance(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    merchant = db_session.get(Merchant, merchant_id)
    merchant.subscription_tier = "starter"
    db_session.commit()

    plan = PLAN_DEFINITIONS["starter"]
    now = datetime.now(timezone.utc)
    for _ in range(plan.included_runs + 5):
        db_session.add(UsageEvent(merchant_id=merchant_id, kind="csv_upload", created_at=now))
    db_session.commit()

    summary = compute_usage_summary(db_session, merchant, now=now)
    assert summary.overage_runs == 5
    assert summary.estimated_overage_cost_gbp == round(5 * plan.overage_price_gbp, 2)


def test_usage_from_prior_calendar_month_does_not_count(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    merchant = db_session.get(Merchant, merchant_id)

    now = datetime.now(timezone.utc)
    last_month = current_period_start(now) - timedelta(days=1)
    db_session.add(UsageEvent(merchant_id=merchant_id, kind="csv_upload", created_at=last_month))
    db_session.commit()

    summary = compute_usage_summary(db_session, merchant, now=now)
    assert summary.insight_runs_used == 0
