"""Member API: creation, listing (with churn column), auth-scoping."""
import pytest


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


def test_create_and_list_members(client, auth_headers):
    create_resp = client.post(
        "/api/v1/members",
        json={"first_name": "Grace", "last_name": "Hopper", "email": "grace@example.com"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    member_id = create_resp.json()["id"]

    list_resp = client.get("/api/v1/members", headers=auth_headers)
    assert list_resp.status_code == 200
    members = list_resp.json()
    assert len(members) == 1
    assert members[0]["id"] == member_id
    # churn-risk column present (acceptance criterion)
    assert "churn_risk_score" in members[0]
    assert "churn_risk_band" in members[0]
    assert members[0]["churn_risk_score"] is not None


def test_list_members_includes_next_visit_prediction_fields(client, auth_headers):
    create_resp = client.post(
        "/api/v1/members",
        json={"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201

    list_resp = client.get("/api/v1/members", headers=auth_headers)
    assert list_resp.status_code == 200
    member = list_resp.json()[0]
    # Fields must be present in the response shape even when there's not
    # enough purchase history yet to predict anything (brand-new member,
    # zero transactions) -- should read as null, not be missing.
    assert "predicted_next_visit_date" in member
    assert "next_visit_days_overdue" in member
    assert member["predicted_next_visit_date"] is None


def test_get_single_member_includes_churn(client, auth_headers):
    create_resp = client.post(
        "/api/v1/members",
        json={"first_name": "Alan", "last_name": "Turing", "email": "alan@example.com"},
        headers=auth_headers,
    )
    member_id = create_resp.json()["id"]

    get_resp = client.get(f"/api/v1/members/{member_id}", headers=auth_headers)
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["churn_risk_band"] in {"low", "medium", "high"}


def test_member_earns_points_via_transaction_endpoint(client, auth_headers):
    create_resp = client.post(
        "/api/v1/members",
        json={"first_name": "Katherine", "last_name": "Johnson", "email": "katherine@example.com"},
        headers=auth_headers,
    )
    member_id = create_resp.json()["id"]
    assert create_resp.json()["points_balance"] == 0

    txn_resp = client.post(
        "/api/v1/transactions",
        json={"member_id": member_id, "amount_gbp": 37.0, "channel": "pos"},
        headers=auth_headers,
    )
    assert txn_resp.status_code == 201
    assert txn_resp.json()["points"] == 37

    member_resp = client.get(f"/api/v1/members/{member_id}", headers=auth_headers)
    assert member_resp.json()["points_balance"] == 37


def test_members_are_scoped_to_merchant(client, auth_headers):
    # Second merchant should not see the first merchant's members.
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Other Co", "email": "other@other.example.com", "password": "pw12345"},
    )
    other_login = client.post(
        "/api/v1/auth/login", json={"email": "other@other.example.com", "password": "pw12345"}
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}

    client.post(
        "/api/v1/members",
        json={"first_name": "Grace", "last_name": "Hopper", "email": "grace@example.com"},
        headers=auth_headers,
    )

    other_list = client.get("/api/v1/members", headers=other_headers)
    assert other_list.json() == []
