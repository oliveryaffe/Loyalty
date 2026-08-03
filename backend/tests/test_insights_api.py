"""HTTP-layer tests for the insights API (app/api/insights.py) --
PLAN_BATCH2.md §5 endpoints + the acceptance criteria in that plan's
"Acceptance criteria" section: CSV upload (success, malformed rows,
missing headers, mint_points toggle, idempotent re-upload), future-value
list/detail, next-best-product, report.csv, auth (401) + merchant
scoping (404 for cross-merchant member ids).
"""
import io

import pytest

from app.config import settings

SAMPLE_CSV = (
    "customer_email,customer_first_name,customer_last_name,transaction_date,amount_usd,"
    "product_category,product_name,channel,external_order_id\n"
    "csv1@example.com,Csv,One,2026-05-01,25.00,beverage,Cold Brew 16oz,pos,API-ORD-1\n"
    "csv2@example.com,Csv,Two,2026-05-02,18.50,bakery,Muffin,pos,API-ORD-2\n"
    "csv1@example.com,Csv,One,2026-05-03,12.00,beverage,Cold Brew 16oz,online,API-ORD-3\n"
)

BAD_HEADER_CSV = "customer_email,transaction_date\ncsv1@example.com,2026-05-01\n"

MALFORMED_ROWS_CSV = (
    "customer_email,transaction_date,amount_usd\n"
    + "".join(f"good{i}@example.com,2026-05-0{(i % 9) + 1},10.00\n" for i in range(1, 11))
    + "bad1@example.com,not-a-date,10.00\n"
    + "bad2@example.com,2026-05-01,not-a-number\n"
)


def _upload(client, headers, content: bytes, filename="upload.csv", mint_points=None):
    params = {}
    if mint_points is not None:
        params["mint_points"] = str(mint_points).lower()
    return client.post(
        "/api/v1/insights/upload",
        headers=headers,
        params=params,
        files={"file": (filename, io.BytesIO(content), "text/csv")},
    )


@pytest.fixture()
def auth_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Insights Test Co", "email": "insights-owner@example.com", "password": "pw123456"},
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "insights-owner@example.com", "password": "pw123456"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_merchant_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Other Co", "email": "other-owner@example.com", "password": "pw123456"},
    )
    resp = client.post("/api/v1/auth/login", json={"email": "other-owner@example.com", "password": "pw123456"})
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def member_id(client, auth_headers):
    resp = client.post(
        "/api/v1/members",
        json={"first_name": "Marie", "last_name": "Curie", "email": "marie@example.com"},
        headers=auth_headers,
    )
    return resp.json()["id"]


# ---------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------


def test_upload_valid_csv_succeeds(client, auth_headers):
    resp = _upload(client, auth_headers, SAMPLE_CSV.encode())
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows_received"] == 3
    assert body["rows_ingested"] == 3
    assert body["rows_failed"] == 0
    assert body["members_created"] == 2
    assert body["errors"] == []


def test_upload_missing_required_header_rejects_whole_file(client, auth_headers):
    resp = _upload(client, auth_headers, BAD_HEADER_CSV.encode())
    assert resp.status_code == 422


def test_upload_malformed_rows_are_skipped_and_reported(client, auth_headers):
    resp = _upload(client, auth_headers, MALFORMED_ROWS_CSV.encode())
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows_ingested"] == 10
    assert body["rows_failed"] == 2
    assert len(body["errors"]) == 2


def test_upload_requires_auth(client):
    resp = client.post(
        "/api/v1/insights/upload",
        files={"file": ("upload.csv", io.BytesIO(SAMPLE_CSV.encode()), "text/csv")},
    )
    assert resp.status_code == 401


def test_upload_default_mint_points_false_leaves_balance_unchanged(client, auth_headers, member_id):
    before = client.get(f"/api/v1/members/{member_id}", headers=auth_headers).json()["points_balance"]

    body = (
        "customer_email,transaction_date,amount_usd,external_order_id\n"
        "marie@example.com,2026-05-01,75.00,BACKFILL-ORD-1\n"
    ).encode()
    resp = _upload(client, auth_headers, body)
    assert resp.status_code == 200
    assert resp.json()["rows_ingested"] == 1

    after = client.get(f"/api/v1/members/{member_id}", headers=auth_headers).json()["points_balance"]
    assert after == before


def test_upload_mint_points_true_increases_balance(client, auth_headers, member_id):
    before = client.get(f"/api/v1/members/{member_id}", headers=auth_headers).json()["points_balance"]

    body = (
        "customer_email,transaction_date,amount_usd,external_order_id\n"
        "marie@example.com,2026-05-01,75.00,MINT-ORD-1\n"
    ).encode()
    resp = _upload(client, auth_headers, body, mint_points=True)
    assert resp.status_code == 200
    assert resp.json()["rows_ingested"] == 1

    after = client.get(f"/api/v1/members/{member_id}", headers=auth_headers).json()["points_balance"]
    assert after == before + 75  # 1:1 points_per_dollar, floored


def test_reuploading_same_file_is_idempotent(client, auth_headers):
    first = _upload(client, auth_headers, SAMPLE_CSV.encode())
    assert first.status_code == 200
    assert first.json()["rows_ingested"] == 3

    second = _upload(client, auth_headers, SAMPLE_CSV.encode())
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["rows_ingested"] == 0
    assert second_body["rows_skipped_duplicate"] == 3


def test_upload_non_csv_file_rejected(client, auth_headers):
    resp = client.post(
        "/api/v1/insights/upload",
        headers=auth_headers,
        files={"file": ("upload.txt", io.BytesIO(b"not a csv"), "text/plain")},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------
# Future value
# ---------------------------------------------------------------------


def test_future_value_list_returns_one_entry_per_member(client, auth_headers, member_id):
    resp = client.get("/api/v1/insights/future-value", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["member_id"] == member_id
    assert entry["predicted_future_value"] >= 0
    assert entry["model_used"] in ("trained", "heuristic")
    assert entry["horizon_days"] == 90


def test_future_value_respects_horizon_days_param(client, auth_headers, member_id):
    resp = client.get("/api/v1/insights/future-value?horizon_days=30", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()[0]["horizon_days"] == 30


def test_future_value_for_member_matches_list_entry(client, auth_headers, member_id):
    resp = client.get(f"/api/v1/insights/future-value/{member_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["member_id"] == member_id


def test_future_value_unknown_member_404s(client, auth_headers):
    resp = client.get("/api/v1/insights/future-value/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404


def test_future_value_cross_merchant_member_404s(client, auth_headers, other_merchant_headers, member_id):
    resp = client.get(f"/api/v1/insights/future-value/{member_id}", headers=other_merchant_headers)
    assert resp.status_code == 404


def test_future_value_requires_auth(client, member_id):
    resp = client.get(f"/api/v1/insights/future-value/{member_id}")
    assert resp.status_code == 401


# ---------------------------------------------------------------------
# Next best product
# ---------------------------------------------------------------------


def test_next_best_product_for_member_with_no_data_returns_empty_or_graceful(client, auth_headers, member_id):
    resp = client.get(f"/api/v1/insights/next-best-product/{member_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_next_best_product_unknown_member_404s(client, auth_headers):
    resp = client.get("/api/v1/insights/next-best-product/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404


def test_next_best_product_flips_to_product_granularity_after_upload(client, auth_headers, member_id):
    # Seed a second member + reward-redemption category signal so the
    # affinity matrix isn't degenerate before upload.
    resp = client.post(
        "/api/v1/rewards",
        json={"name": "Free Coffee", "category": "beverage", "points_cost": 50, "tier_required": "bronze"},
        headers=auth_headers,
    )
    reward_id = resp.json()["id"]
    client.post(
        "/api/v1/transactions", json={"member_id": member_id, "amount_usd": 100}, headers=auth_headers
    )
    client.post(
        "/api/v1/rewards/redeem",
        json={"member_id": member_id, "reward_id": reward_id},
        headers=auth_headers,
    )

    before = client.get(f"/api/v1/insights/next-best-product/{member_id}", headers=auth_headers).json()
    for r in before:
        assert r["data_granularity"] == "category"
        assert r["product_name"] is None

    body = (
        "customer_email,transaction_date,amount_usd,product_category,product_name,external_order_id\n"
        "marie@example.com,2026-05-01,15.00,merchandise,Ceramic Mug,NBP-ORD-1\n"
        "marie@example.com,2026-05-02,15.00,merchandise,Ceramic Mug,NBP-ORD-2\n"
    ).encode()
    upload_resp = _upload(client, auth_headers, body)
    assert upload_resp.status_code == 200
    assert upload_resp.json()["rows_ingested"] == 2

    after = client.get(f"/api/v1/insights/next-best-product/{member_id}", headers=auth_headers).json()
    assert len(after) > 0
    assert all(r["data_granularity"] == "product" for r in after)


# ---------------------------------------------------------------------
# report.csv
# ---------------------------------------------------------------------


def test_report_csv_returns_valid_csv_with_one_row_per_member(client, auth_headers, member_id):
    resp = client.get("/api/v1/insights/report.csv", headers=auth_headers)
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]

    import csv as csv_module

    lines = resp.text.strip().splitlines()
    reader = csv_module.reader(lines)
    rows = list(reader)
    header = rows[0]
    assert header == [
        "member_id", "first_name", "last_name", "email", "tier",
        "predicted_future_value", "horizon_days", "model_used",
        "next_best_category", "next_best_product", "next_best_score",
    ]
    data_rows = rows[1:]
    assert len(data_rows) == 1
    assert data_rows[0][0] == member_id


def test_report_csv_requires_auth(client):
    resp = client.get("/api/v1/insights/report.csv")
    assert resp.status_code == 401
