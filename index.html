"""Merchant auth: signup, JWT login, invalid credentials rejected."""


def test_signup_then_login_succeeds(client):
    signup_resp = client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Acme Retail", "email": "admin@acme.example.com", "password": "correct-horse"},
    )
    assert signup_resp.status_code == 201

    login_resp = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@acme.example.com", "password": "correct-horse"},
    )
    assert login_resp.status_code == 200
    body = login_resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


def test_login_with_wrong_password_is_rejected(client):
    client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Acme Retail", "email": "admin2@acme.example.com", "password": "correct-horse"},
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "admin2@acme.example.com", "password": "wrong-password"},
    )
    assert resp.status_code == 401


def test_login_with_unknown_email_is_rejected(client):
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@nowhere.example.com", "password": "whatever"},
    )
    assert resp.status_code == 401


def test_protected_endpoint_requires_token(client):
    resp = client.get("/api/v1/members")
    assert resp.status_code == 401


def test_protected_endpoint_rejects_garbage_token(client):
    resp = client.get("/api/v1/members", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
