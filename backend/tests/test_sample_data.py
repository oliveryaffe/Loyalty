"""Per-vertical sample data (app/services/sample_data.py) + the
POST /insights/sample-data endpoint: safety gate (never touch real data),
vertical-specific product/reward differentiation, and clean regeneration
when switching verticals.
"""
from __future__ import annotations

import io

import pytest

from app.db.models import FraudAlert, Member, RewardCatalogItem, Transaction
from app.services.sample_data import (
    SAMPLE_DATA_BUSINESS_TYPES,
    VERTICAL_PROFILES,
    generate_sample_dataset,
    has_real_data,
    is_viewing_sample_data,
)

SAMPLE_CSV = "customer_email,transaction_date,amount_gbp\nreal1@example.com,2026-05-01,25.00\n"


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Acme Retail", "email": "sampledata-owner@acme.example.com", "password": "s3cret-pw"},
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "sampledata-owner@acme.example.com", "password": "s3cret-pw"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def _load_sample(client, headers, business_type="coffee_shop"):
    return client.post(
        "/api/v1/insights/sample-data", json={"business_type": business_type}, headers=headers
    )


# ---------------------------------------------------------------------------
# Service-level
# ---------------------------------------------------------------------------


def test_every_business_type_has_a_profile():
    assert set(SAMPLE_DATA_BUSINESS_TYPES) == {"coffee_shop", "restaurant", "barber_salon", "retail"}


def test_generate_creates_members_transactions_and_rewards(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    from app.db.models import Merchant

    merchant = db_session.get(Merchant, merchant_id)
    result = generate_sample_dataset(db_session, merchant, "coffee_shop", member_count=25)
    db_session.commit()

    assert result.members_created == 25
    assert result.transactions_created > 0
    assert result.rewards_created == len(VERTICAL_PROFILES["coffee_shop"].rewards)

    members = db_session.query(Member).filter(Member.merchant_id == merchant_id).all()
    assert all(m.is_sample is True for m in members)
    rewards = db_session.query(RewardCatalogItem).filter(RewardCatalogItem.merchant_id == merchant_id).all()
    assert all(r.is_sample is True for r in rewards)


def test_vertical_products_are_distinct(client, db_session, admin_headers):
    """Different verticals must actually produce different product
    categories -- not just a relabeled coffee shop."""
    merchant_id = _merchant_id(client, admin_headers)
    from app.db.models import Merchant

    merchant = db_session.get(Merchant, merchant_id)
    generate_sample_dataset(db_session, merchant, "barber_salon", member_count=40)
    db_session.commit()

    categories = {
        row[0]
        for row in db_session.query(Transaction.product_category)
        .join(Member, Transaction.member_id == Member.id)
        .filter(Member.merchant_id == merchant_id)
        .distinct()
        .all()
    }
    assert categories <= {"haircut", "beard", "colour", "treatment"}
    assert "beverage" not in categories
    assert "bakery" not in categories


def test_vertical_rewards_are_distinct():
    coffee_names = {name for name, *_ in VERTICAL_PROFILES["coffee_shop"].rewards}
    retail_names = {name for name, *_ in VERTICAL_PROFILES["retail"].rewards}
    assert coffee_names.isdisjoint(retail_names)


def test_regenerating_clears_prior_sample_data(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    from app.db.models import Merchant

    merchant = db_session.get(Merchant, merchant_id)
    generate_sample_dataset(db_session, merchant, "coffee_shop", member_count=20)
    db_session.commit()
    first_member_ids = {m.id for m in db_session.query(Member).filter(Member.merchant_id == merchant_id).all()}

    generate_sample_dataset(db_session, merchant, "retail", member_count=20)
    db_session.commit()
    second_member_ids = {m.id for m in db_session.query(Member).filter(Member.merchant_id == merchant_id).all()}

    # No overlap -- the coffee-shop sample members were fully cleared, not
    # appended to.
    assert first_member_ids.isdisjoint(second_member_ids)

    categories = {
        row[0]
        for row in db_session.query(Transaction.product_category)
        .join(Member, Transaction.member_id == Member.id)
        .filter(Member.merchant_id == merchant_id)
        .distinct()
        .all()
    }
    assert "beverage" not in categories  # no leftover coffee-shop transactions

    rewards = db_session.query(RewardCatalogItem).filter(RewardCatalogItem.merchant_id == merchant_id).all()
    assert len(rewards) == len(VERTICAL_PROFILES["retail"].rewards)


def test_regenerating_does_not_orphan_fraud_alerts(client, db_session, admin_headers):
    """Sample data intentionally injects fraud-like transactions -- fraud
    alerts get generated against them once scored. Regenerating for a
    different vertical must clean those up too, not leave dangling rows
    referencing deleted members."""
    merchant_id = _merchant_id(client, admin_headers)
    from app.db.models import Merchant

    merchant = db_session.get(Merchant, merchant_id)
    generate_sample_dataset(db_session, merchant, "coffee_shop", member_count=60)
    db_session.commit()

    # Manually score fraud so alerts exist against sample transactions
    # (mirrors what GET /fraud-alerts does on the real path).
    from app.ai.fraud_detector import run_fraud_detection

    run_fraud_detection(db_session, merchant_id)
    db_session.commit()
    assert db_session.query(FraudAlert).count() > 0

    # Should not raise (no FK violation) and should leave zero fraud
    # alerts behind once the sample members that owned them are gone.
    generate_sample_dataset(db_session, merchant, "restaurant", member_count=20)
    db_session.commit()

    remaining_member_ids = {m.id for m in db_session.query(Member).filter(Member.merchant_id == merchant_id).all()}
    orphaned = [
        a for a in db_session.query(FraudAlert).all() if a.member_id not in remaining_member_ids
    ]
    assert orphaned == []


def test_has_real_data_false_for_sample_only(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    from app.db.models import Merchant

    merchant = db_session.get(Merchant, merchant_id)
    generate_sample_dataset(db_session, merchant, "coffee_shop", member_count=15)
    db_session.commit()
    assert has_real_data(db_session, merchant_id) is False


def test_generate_refuses_when_real_data_exists(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    from app.db.models import Merchant

    merchant = db_session.get(Merchant, merchant_id)
    db_session.add(Member(merchant_id=merchant_id, first_name="Real", last_name="Customer", email="real@example.com"))
    db_session.commit()

    with pytest.raises(ValueError):
        generate_sample_dataset(db_session, merchant, "coffee_shop")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_endpoint_generates_sample_data(client, admin_headers):
    resp = _load_sample(client, admin_headers, "barber_salon")
    assert resp.status_code == 200
    body = resp.json()
    assert body["business_type"] == "barber_salon"
    assert body["members_created"] > 0
    assert body["transactions_created"] > 0
    assert body["rewards_created"] > 0


def test_endpoint_rejects_unknown_business_type(client, admin_headers):
    resp = _load_sample(client, admin_headers, "spaceship_repair")
    assert resp.status_code == 422


def test_endpoint_409s_when_real_data_exists(client, admin_headers):
    client.post(
        "/api/v1/insights/upload",
        headers=admin_headers,
        files={"file": ("upload.csv", io.BytesIO(SAMPLE_CSV.encode()), "text/csv")},
    )
    resp = _load_sample(client, admin_headers, "coffee_shop")
    assert resp.status_code == 409


def test_endpoint_requires_admin_role(client, admin_headers):
    client.post(
        "/api/v1/team/invite",
        json={"email": "sampledata-teammate@acme.example.com", "password": "teammate-pw1", "role": "member"},
        headers=admin_headers,
    )
    login = client.post(
        "/api/v1/auth/login", json={"email": "sampledata-teammate@acme.example.com", "password": "teammate-pw1"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = _load_sample(client, member_headers, "coffee_shop")
    assert resp.status_code == 403


def test_endpoint_requires_auth(client):
    resp = client.post("/api/v1/insights/sample-data", json={"business_type": "coffee_shop"})
    assert resp.status_code == 401


def test_endpoint_reload_switches_vertical_cleanly(client, admin_headers):
    first = _load_sample(client, admin_headers, "coffee_shop")
    assert first.status_code == 200

    second = _load_sample(client, admin_headers, "restaurant")
    assert second.status_code == 200

    members_resp = client.get("/api/v1/members", headers=admin_headers)
    assert members_resp.status_code == 200
    # Still no real data, so a third reload must still be allowed.
    third = _load_sample(client, admin_headers, "retail")
    assert third.status_code == 200


def test_sample_data_does_not_count_as_billable_usage(client, admin_headers):
    _load_sample(client, admin_headers, "coffee_shop")
    usage_resp = client.get("/api/v1/billing/usage", headers=admin_headers)
    assert usage_resp.json()["insight_runs_used"] == 0


# ---------------------------------------------------------------------------
# GET /insights/sample-data/status
# ---------------------------------------------------------------------------


def test_status_false_when_no_data_at_all(client, admin_headers):
    resp = client.get("/api/v1/insights/sample-data/status", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["is_sample_data"] is False


def test_status_true_after_loading_sample_data(client, admin_headers):
    _load_sample(client, admin_headers, "coffee_shop")
    resp = client.get("/api/v1/insights/sample-data/status", headers=admin_headers)
    assert resp.json()["is_sample_data"] is True


def test_status_false_once_real_data_uploaded(client, admin_headers):
    _load_sample(client, admin_headers, "coffee_shop")
    client.post(
        "/api/v1/insights/upload",
        headers=admin_headers,
        files={"file": ("upload.csv", io.BytesIO(SAMPLE_CSV.encode()), "text/csv")},
    )
    resp = client.get("/api/v1/insights/sample-data/status", headers=admin_headers)
    assert resp.json()["is_sample_data"] is False


def test_real_upload_purges_sample_data_instead_of_mixing(client, db_session, admin_headers):
    """A real CSV upload must fully replace sample data, not sit alongside
    it -- otherwise fake sample members would pollute a real merchant's
    churn/future-value analytics forever."""
    merchant_id = _merchant_id(client, admin_headers)
    load_resp = _load_sample(client, admin_headers, "coffee_shop")
    sample_member_count = load_resp.json()["members_created"]
    assert sample_member_count > 0

    client.post(
        "/api/v1/insights/upload",
        headers=admin_headers,
        files={"file": ("upload.csv", io.BytesIO(SAMPLE_CSV.encode()), "text/csv")},
    )

    remaining_sample = (
        db_session.query(Member)
        .filter(Member.merchant_id == merchant_id, Member.is_sample.is_(True))
        .count()
    )
    assert remaining_sample == 0

    all_members = db_session.query(Member).filter(Member.merchant_id == merchant_id).all()
    assert len(all_members) == 1
    assert all_members[0].email == "real1@example.com"


def test_is_viewing_sample_data_service_level(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    from app.db.models import Merchant

    merchant = db_session.get(Merchant, merchant_id)
    assert is_viewing_sample_data(db_session, merchant_id) is False

    generate_sample_dataset(db_session, merchant, "coffee_shop", member_count=10)
    db_session.commit()
    assert is_viewing_sample_data(db_session, merchant_id) is True

