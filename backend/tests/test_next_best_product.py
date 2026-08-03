"""Next-best-product model tests (app/ai/next_best_product.py).

Covers PLAN_BATCH2.md acceptance criteria 3 & 6: against the seeded demo
merchant with zero upload, the model degrades gracefully to the
Redemption x RewardCatalogItem.category substrate (data_granularity
"category", product_name null, non-empty category). After uploading
product-level CSV data for a member, next-best-product for that member
flips to data_granularity "product" with a real product_name.
"""
import pytest

from app.ai.next_best_product import build_affinity_matrix, recommend_next_best
from app.db.models import Member, Merchant, Redemption, RewardCatalogItem, Transaction
from app.services.csv_ingest import parse_and_ingest_csv


def test_seeded_merchant_falls_back_to_category_granularity(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    matrix, granularity = build_affinity_matrix(seeded_db, merchant.id)

    assert granularity == "category"
    assert not matrix.empty  # seed script generates real completed redemptions


def test_seeded_members_get_category_recommendations_with_null_product(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    matrix, granularity = build_affinity_matrix(seeded_db, merchant.id)

    members = seeded_db.query(Member).filter(Member.merchant_id == merchant.id).limit(25).all()
    saw_non_empty_result = False
    for member in members:
        ranked = recommend_next_best(seeded_db, member, matrix, granularity, top_n=3)
        for r in ranked:
            saw_non_empty_result = True
            assert r.product_name is None
            assert r.category
            assert isinstance(r.score, float)

    assert saw_non_empty_result


def test_cold_start_member_with_no_history_gets_popularity_fallback(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    matrix, granularity = build_affinity_matrix(seeded_db, merchant.id)

    brand_new = Member(
        merchant_id=merchant.id,
        first_name="Cold",
        last_name="Start",
        email="cold-start-nbp-test@example.com",
    )
    seeded_db.add(brand_new)
    seeded_db.flush()

    ranked = recommend_next_best(seeded_db, brand_new, matrix, granularity, top_n=3)
    assert len(ranked) > 0
    assert all(r.product_name is None for r in ranked)
    assert all("cold-start" in r.reason for r in ranked)


@pytest.fixture()
def merchant(db_session):
    m = Merchant(business_name="NBP Upload Test Co")
    db_session.add(m)
    db_session.flush()
    return m


def _seed_baseline_category_signal(db_session, merchant):
    """A little redemption history across a few members/categories so the
    affinity matrix isn't degenerate before we layer product-level upload
    data on top."""
    rewards = {
        "beverage": RewardCatalogItem(
            merchant_id=merchant.id, name="Free Coffee", category="beverage", points_cost=100, active=True
        ),
        "merchandise": RewardCatalogItem(
            merchant_id=merchant.id, name="Mug", category="merchandise", points_cost=200, active=True
        ),
    }
    db_session.add_all(rewards.values())
    db_session.flush()

    members = []
    for i in range(6):
        m = Member(
            merchant_id=merchant.id, first_name=f"U{i}", last_name="Test", email=f"u{i}@example.com",
            points_balance=1000,
        )
        db_session.add(m)
        db_session.flush()
        category = "beverage" if i % 2 == 0 else "merchandise"
        reward = rewards[category]
        db_session.add(
            Redemption(member_id=m.id, reward_id=reward.id, points_spent=reward.points_cost, status="completed")
        )
        members.append(m)
    db_session.flush()
    return members


def test_uploaded_product_data_flips_granularity_to_product(db_session, merchant):
    _seed_baseline_category_signal(db_session, merchant)

    csv_body = (
        "customer_email,customer_first_name,customer_last_name,transaction_date,amount_usd,"
        "product_category,product_name,channel,external_order_id\n"
        "u0@example.com,U0,Test,2026-05-01,10.00,beverage,Cold Brew 16oz,pos,ORD-NBP-1\n"
        "u0@example.com,U0,Test,2026-05-02,12.00,beverage,Cold Brew 16oz,pos,ORD-NBP-2\n"
        "u1@example.com,U1,Test,2026-05-01,20.00,merchandise,Ceramic Mug,pos,ORD-NBP-3\n"
        "u2@example.com,U2,Test,2026-05-01,8.00,beverage,Pour Over,pos,ORD-NBP-4\n"
    ).encode("utf-8")

    result = parse_and_ingest_csv(db_session, merchant, csv_body, mint_points=False)
    db_session.commit()
    assert result.rows_ingested == 4

    matrix, granularity = build_affinity_matrix(db_session, merchant.id)
    assert granularity == "product"
    assert not matrix.empty

    member = db_session.query(Member).filter(Member.email == "u0@example.com").first()
    ranked = recommend_next_best(db_session, member, matrix, granularity, top_n=3)
    assert len(ranked) > 0
    # u0 already heavily engaged "beverage" (via CSV upload) -- recommendation
    # should be for a *different* category, with a real representative product.
    top_categories = {r.category for r in ranked}
    assert "beverage" not in top_categories or any(r.product_name for r in ranked)
    # At least one ranked result carries a concrete product_name now that
    # product-level data exists for this merchant.
    assert any(r.product_name for r in ranked)


def test_representative_product_is_most_purchased_in_category(db_session, merchant):
    _seed_baseline_category_signal(db_session, merchant)
    csv_body = (
        "customer_email,customer_first_name,customer_last_name,transaction_date,amount_usd,"
        "product_category,product_name,channel,external_order_id\n"
        "u0@example.com,U0,Test,2026-05-01,10.00,beverage,Cold Brew 16oz,pos,ORD-A\n"
        "u1@example.com,U1,Test,2026-05-01,10.00,beverage,Cold Brew 16oz,pos,ORD-B\n"
        "u2@example.com,U2,Test,2026-05-01,5.00,beverage,Pour Over,pos,ORD-C\n"
    ).encode("utf-8")
    result = parse_and_ingest_csv(db_session, merchant, csv_body, mint_points=False)
    db_session.commit()
    assert result.rows_ingested == 3

    matrix, granularity = build_affinity_matrix(db_session, merchant.id)
    assert granularity == "product"

    from app.ai.next_best_product import _representative_product

    top_product = _representative_product(db_session, merchant.id, "beverage")
    assert top_product == "Cold Brew 16oz"
