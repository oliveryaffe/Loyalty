"""Business-level "so what" report (app/services/business_insights.py) --
ties churn risk + future value into a revenue-at-risk headline, a trend
against the last MerchantMetricSnapshot, the dominant reason behind the
current at-risk cohort, and which reward/purchase categories are actually
associated with higher-value customers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import (
    Member,
    Merchant,
    MerchantMetricSnapshot,
    Redemption,
    RewardCatalogItem,
    Transaction,
    TransactionType,
)
from app.services.business_insights import compute_business_insights

NOW = datetime.now(timezone.utc)


def _merchant(db_session, name="Business Insights Test Co") -> Merchant:
    m = Merchant(business_name=name)
    db_session.add(m)
    db_session.flush()
    return m


def _member(db_session, merchant_id, label, days_since_activity, points_balance=100) -> Member:
    member = Member(
        merchant_id=merchant_id,
        first_name=label,
        last_name="Test",
        email=f"{label.lower()}@example.com",
        points_balance=points_balance,
        last_activity_at=NOW - timedelta(days=days_since_activity),
    )
    db_session.add(member)
    db_session.flush()
    return member


def _earn_txn(db_session, member_id, amount, days_ago, category=None, product_name=None):
    db_session.add(
        Transaction(
            member_id=member_id,
            type=TransactionType.EARN.value,
            amount_gbp=amount,
            points=int(amount),
            created_at=NOW - timedelta(days=days_ago),
            product_category=category,
            product_name=product_name,
        )
    )


@pytest.fixture()
def healthy_and_at_risk_merchant(db_session):
    """A handful of engaged, recently-active, high-spend members, plus a
    cluster of members who all went quiet 150+ days ago (below
    MIN_MEMBERS_WITH_REPEAT_VISITS=20 total, so this always scores under
    DEFAULT_CALIBRATION -- recency_saturation_days=90 -- keeping the
    "who's high risk" outcome deterministic for these tests)."""
    m = _merchant(db_session)
    for i in range(6):
        member = _member(db_session, m.id, f"Healthy{i}", days_since_activity=2)
        for w in range(6):
            _earn_txn(db_session, member.id, amount=25.0, days_ago=w * 14 + 2, category="beverage")
    for i in range(4):
        # Established purchase history (so avg_order_value/frequency are
        # non-zero -- a member whose *only* purchase was outside the
        # calibration lookback window would score 0 predicted future value
        # regardless of churn risk, which is correct heuristic behaviour
        # but not what "at risk, still has value to protect" is meant to
        # exercise here) whose most recent visit is close to (but inside)
        # the 90-day recency saturation point -- high churn risk without
        # falling out of the 120-day future-value lookback window.
        member = _member(db_session, m.id, f"AtRisk{i}", days_since_activity=85)
        _earn_txn(db_session, member.id, amount=20.0, days_ago=85, category="food")
        _earn_txn(db_session, member.id, amount=20.0, days_ago=100, category="food")
        _earn_txn(db_session, member.id, amount=20.0, days_ago=115, category="food")
    db_session.commit()
    return m


def test_revenue_at_risk_ties_churn_and_future_value(db_session, healthy_and_at_risk_merchant):
    report = compute_business_insights(db_session, healthy_and_at_risk_merchant)

    assert report.total_members == 10
    assert report.revenue_at_risk.total_future_value_gbp > 0
    # The 4 long-inactive members should be driving a non-zero, non-total
    # share of the book's predicted value -- not 0% (nobody at risk) and
    # not 100% (everybody at risk, given 6 clearly healthy members exist).
    assert report.revenue_at_risk.at_risk_share is not None
    assert 0.0 < report.revenue_at_risk.at_risk_share < 1.0
    assert "£" in report.revenue_at_risk.headline


def test_revenue_at_risk_headline_when_no_members(db_session):
    m = _merchant(db_session, "Empty Co")
    db_session.commit()

    report = compute_business_insights(db_session, m)

    assert report.total_members == 0
    assert report.revenue_at_risk.at_risk_share is None
    assert "Not enough" in report.revenue_at_risk.headline


def test_churn_driver_identifies_recency_as_dominant(db_session, healthy_and_at_risk_merchant):
    report = compute_business_insights(db_session, healthy_and_at_risk_merchant)

    assert report.churn_driver is not None
    assert report.churn_driver.dominant_driver == "recency"
    assert report.churn_driver.share_of_high_risk > 0
    assert "haven't been back" in report.churn_driver.headline or "days" in report.churn_driver.headline


def test_churn_driver_is_none_when_nobody_is_high_risk(db_session):
    m = _merchant(db_session, "All Healthy Co")
    for i in range(5):
        member = _member(db_session, m.id, f"Healthy{i}", days_since_activity=1)
        for w in range(6):
            _earn_txn(db_session, member.id, amount=30.0, days_ago=w * 14 + 1)
    db_session.commit()

    report = compute_business_insights(db_session, m)

    assert report.churn_driver is None


def test_trend_is_none_on_first_call(db_session, healthy_and_at_risk_merchant):
    report = compute_business_insights(db_session, healthy_and_at_risk_merchant)

    assert report.trend is None
    # But a snapshot should now exist for next time.
    snapshots = (
        db_session.query(MerchantMetricSnapshot)
        .filter(MerchantMetricSnapshot.merchant_id == healthy_and_at_risk_merchant.id)
        .all()
    )
    assert len(snapshots) == 1


def test_trend_appears_once_a_prior_snapshot_exists(db_session, healthy_and_at_risk_merchant):
    compute_business_insights(db_session, healthy_and_at_risk_merchant)
    db_session.commit()

    # Backdate the snapshot so the next call is past SNAPSHOT_MIN_INTERVAL_DAYS,
    # and tweak its recorded high_risk_count so the delta is predictable.
    snapshot = (
        db_session.query(MerchantMetricSnapshot)
        .filter(MerchantMetricSnapshot.merchant_id == healthy_and_at_risk_merchant.id)
        .one()
    )
    snapshot.captured_at = NOW - timedelta(days=10)
    snapshot.high_risk_count = 1
    db_session.commit()

    report = compute_business_insights(db_session, healthy_and_at_risk_merchant)

    assert report.trend is not None
    assert report.trend.days_since_previous >= 9
    # 4 at-risk members now vs. 1 recorded previously -> delta of +3.
    assert report.trend.high_risk_count_delta == 3
    assert "up 3" in report.trend.headline


def test_snapshot_not_recaptured_within_min_interval(db_session, healthy_and_at_risk_merchant):
    compute_business_insights(db_session, healthy_and_at_risk_merchant)
    db_session.commit()
    compute_business_insights(db_session, healthy_and_at_risk_merchant)
    db_session.commit()

    snapshots = (
        db_session.query(MerchantMetricSnapshot)
        .filter(MerchantMetricSnapshot.merchant_id == healthy_and_at_risk_merchant.id)
        .all()
    )
    assert len(snapshots) == 1


def test_category_performance_falls_back_to_purchase_when_no_redemptions(db_session, healthy_and_at_risk_merchant):
    report = compute_business_insights(db_session, healthy_and_at_risk_merchant)

    assert report.top_categories
    assert all(c.source == "purchase" for c in report.top_categories)
    assert all(c.engaged_members >= 3 for c in report.top_categories)


def test_category_performance_prefers_redemptions_when_present(db_session):
    m = _merchant(db_session, "Redemption Categories Co")
    reward = RewardCatalogItem(merchant_id=m.id, name="Free Coffee", category="beverage", points_cost=100, active=True)
    db_session.add(reward)
    db_session.flush()

    for i in range(6):
        member = _member(db_session, m.id, f"Redeemer{i}", days_since_activity=2)
        for w in range(4):
            _earn_txn(db_session, member.id, amount=20.0, days_ago=w * 14 + 2, category="snack")
        db_session.add(
            Redemption(member_id=member.id, reward_id=reward.id, points_spent=100, status="completed")
        )
    db_session.commit()

    report = compute_business_insights(db_session, m)

    assert report.top_categories
    assert all(c.source == "redemption" for c in report.top_categories)
    assert any(c.category == "beverage" for c in report.top_categories)


def test_category_performance_excludes_categories_below_min_members(db_session):
    m = _merchant(db_session, "Sparse Categories Co")
    member = _member(db_session, m.id, "Solo", days_since_activity=2)
    _earn_txn(db_session, member.id, amount=20.0, days_ago=2, category="rare_category")
    # Enough other members so the merchant-wide baseline isn't itself degenerate.
    for i in range(4):
        other = _member(db_session, m.id, f"Other{i}", days_since_activity=3)
        _earn_txn(db_session, other.id, amount=15.0, days_ago=3, category="common_category")
        _earn_txn(db_session, other.id, amount=15.0, days_ago=17, category="common_category")
        _earn_txn(db_session, other.id, amount=15.0, days_ago=31, category="common_category")
        _earn_txn(db_session, other.id, amount=15.0, days_ago=45, category="common_category")
    db_session.commit()

    report = compute_business_insights(db_session, m)

    categories_shown = {c.category for c in report.top_categories}
    assert "rare_category" not in categories_shown  # only 1 engaged member -- below MIN_MEMBERS_FOR_CATEGORY_STAT


def test_get_business_insights_endpoint(client, db_session):
    client.post(
        "/api/v1/auth/signup",
        json={
            "business_name": "Insights Endpoint Test Co",
            "email": "biz-insights@example.com",
            "password": "s3cret-pw",
        },
    )
    login = client.post(
        "/api/v1/auth/login", json={"email": "biz-insights@example.com", "password": "s3cret-pw"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    merchant_id = client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]

    for i in range(3):
        member = _member(db_session, merchant_id, f"Endpoint{i}", days_since_activity=2)
        _earn_txn(db_session, member.id, amount=20.0, days_ago=2, category="beverage")
    db_session.commit()

    resp = client.get("/api/v1/insights/business-insights", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "revenue_at_risk" in body
    assert "headline" in body["revenue_at_risk"]
    assert body["trend"] is None  # first call ever for this merchant

    snapshots = (
        db_session.query(MerchantMetricSnapshot).filter(MerchantMetricSnapshot.merchant_id == merchant_id).all()
    )
    assert len(snapshots) == 1
