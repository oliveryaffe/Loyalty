"""Transaction ingestion + reward redemption workflow, via the HTTP API."""
import pytest

from app.config import settings


@pytest.fixture()
def auth_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Acme Retail", "email": "owner@acme.example.com", "password": "s3cret-pw"},
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "owner@acme.example.com", "password": "s3cret-pw"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def member_id(client, auth_headers):
    resp = client.post(
        "/api/v1/members",
        json={"first_name": "Rosalind", "last_name": "Franklin", "email": "rosalind@example.com"},
        headers=auth_headers,
    )
    return resp.json()["id"]


@pytest.fixture()
def reward_id(client, auth_headers):
    resp = client.post(
        "/api/v1/rewards",
        json={"name": "Free Coffee", "points_cost": 100, "tier_required": "bronze"},
        headers=auth_headers,
    )
    return resp.json()["id"]


def test_negative_or_zero_amount_rejected(client, auth_headers, member_id):
    resp = client.post(
        "/api/v1/transactions",
        json={"member_id": member_id, "amount_usd": -5},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_transaction_amount_over_max_is_rejected(client, auth_headers, member_id):
    resp = client.post(
        "/api/v1/transactions",
        json={"member_id": member_id, "amount_usd": settings.max_transaction_amount_usd + 0.01},
        headers=auth_headers,
    )
    assert resp.status_code == 422

    member_resp = client.get(f"/api/v1/members/{member_id}", headers=auth_headers)
    assert member_resp.json()["points_balance"] == 0


def test_transaction_amount_at_max_is_accepted(client, auth_headers, member_id):
    resp = client.post(
        "/api/v1/transactions",
        json={"member_id": member_id, "amount_usd": settings.max_transaction_amount_usd},
        headers=auth_headers,
    )
    assert resp.status_code == 201


def test_transaction_for_unknown_member_404s(client, auth_headers):
    resp = client.post(
        "/api/v1/transactions",
        json={"member_id": "does-not-exist", "amount_usd": 10},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_redeem_reward_member_can_afford(client, auth_headers, member_id, reward_id):
    client.post(
        "/api/v1/transactions",
        json={"member_id": member_id, "amount_usd": 150},
        headers=auth_headers,
    )
    resp = client.post(
        "/api/v1/rewards/redeem",
        json={"member_id": member_id, "reward_id": reward_id},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["points_spent"] == 100

    member_resp = client.get(f"/api/v1/members/{member_id}", headers=auth_headers)
    assert member_resp.json()["points_balance"] == 50


def test_redeem_reward_rejected_when_balance_insufficient(client, auth_headers, member_id, reward_id):
    # Member has 0 points, reward costs 100.
    resp = client.post(
        "/api/v1/rewards/redeem",
        json={"member_id": member_id, "reward_id": reward_id},
        headers=auth_headers,
    )
    assert resp.status_code == 400

    member_resp = client.get(f"/api/v1/members/{member_id}", headers=auth_headers)
    assert member_resp.json()["points_balance"] == 0


def test_list_transactions_for_member(client, auth_headers, member_id):
    client.post(
        "/api/v1/transactions", json={"member_id": member_id, "amount_usd": 10}, headers=auth_headers
    )
    client.post(
        "/api/v1/transactions", json={"member_id": member_id, "amount_usd": 20}, headers=auth_headers
    )
    resp = client.get(f"/api/v1/transactions?member_id={member_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2
