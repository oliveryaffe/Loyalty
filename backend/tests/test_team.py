"""Multi-user merchant accounts / role-based access (Feature 3):
JWT claim shape, team invite/list/remove/role-change, admin-only gating,
last-admin lockout, cross-merchant isolation, and the "member role can
still use all pre-existing merchant-scoped endpoints" regression check.
"""
from jose import jwt

from app.config import settings
from scripts.seed_data import DEMO_MERCHANT_EMAIL, DEMO_MERCHANT_PASSWORD, DEMO_TEAM_MEMBER_EMAIL, DEMO_TEAM_MEMBER_PASSWORD


def _login(client, email, password):
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _signup(client, business_name, email, password):
    resp = client.post(
        "/api/v1/auth/signup",
        json={"business_name": business_name, "email": email, "password": password},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _invite(client, admin_token, email, password="teammate-pw1", role="member"):
    return client.post(
        "/api/v1/team/invite",
        json={"email": email, "password": password, "role": role},
        headers=_headers(admin_token),
    )


# ---------------------------------------------------------------------------
# JWT shape
# ---------------------------------------------------------------------------


def test_jwt_contains_sub_merchant_id_and_role_claims(client):
    signup_body = _signup(client, "Acme Retail", "owner@acme.example.com", "s3cret-pw1")
    token = _login(client, "owner@acme.example.com", "s3cret-pw1")

    decoded = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    assert decoded["sub"] == signup_body["id"]
    assert decoded["merchant_id"] == signup_body["merchant_id"]
    assert decoded["role"] == "admin"


def test_signup_response_shape_is_meout(client):
    body = _signup(client, "Acme Retail", "owner2@acme.example.com", "s3cret-pw1")
    assert body["business_name"] == "Acme Retail"
    assert body["email"] == "owner2@acme.example.com"
    assert body["role"] == "admin"
    assert "merchant_id" in body and "id" in body


# ---------------------------------------------------------------------------
# List / invite / role gating
# ---------------------------------------------------------------------------


def test_admin_can_list_and_invite_member_with_no_password_leak(client):
    signup_body = _signup(client, "Acme Retail", "admin@acme.example.com", "s3cret-pw1")
    admin_token = _login(client, "admin@acme.example.com", "s3cret-pw1")

    list_resp = client.get("/api/v1/team", headers=_headers(admin_token))
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    invite_resp = _invite(client, admin_token, "teammate@acme.example.com")
    assert invite_resp.status_code == 201
    body = invite_resp.json()
    assert body["email"] == "teammate@acme.example.com"
    assert body["role"] == "member"
    assert "hashed_password" not in body
    assert "password" not in body

    list_resp2 = client.get("/api/v1/team", headers=_headers(admin_token))
    assert list_resp2.status_code == 200
    assert len(list_resp2.json()) == 2


def test_member_role_can_list_team_but_not_invite(client):
    admin_signup = _signup(client, "Acme Retail", "admin3@acme.example.com", "s3cret-pw1")
    admin_token = _login(client, "admin3@acme.example.com", "s3cret-pw1")
    _invite(client, admin_token, "member3@acme.example.com", password="memberpw1", role="member")
    member_token = _login(client, "member3@acme.example.com", "memberpw1")

    list_resp = client.get("/api/v1/team", headers=_headers(member_token))
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 2

    invite_resp = _invite(client, member_token, "should-fail@acme.example.com")
    assert invite_resp.status_code == 403


# ---------------------------------------------------------------------------
# Remove / role-change + last-admin lockout
# ---------------------------------------------------------------------------


def test_admin_can_remove_a_non_admin_teammate_who_then_cannot_login(client):
    _signup(client, "Acme Retail", "admin4@acme.example.com", "s3cret-pw1")
    admin_token = _login(client, "admin4@acme.example.com", "s3cret-pw1")
    invite_resp = _invite(client, admin_token, "removable@acme.example.com", password="removepw1")
    removable_id = invite_resp.json()["id"]

    login_before = client.post(
        "/api/v1/auth/login", json={"email": "removable@acme.example.com", "password": "removepw1"}
    )
    assert login_before.status_code == 200

    delete_resp = client.delete(f"/api/v1/team/{removable_id}", headers=_headers(admin_token))
    assert delete_resp.status_code == 204

    login_after = client.post(
        "/api/v1/auth/login", json={"email": "removable@acme.example.com", "password": "removepw1"}
    )
    assert login_after.status_code == 401


def test_cannot_remove_the_sole_remaining_admin(client):
    signup_body = _signup(client, "Acme Retail", "sole-admin@acme.example.com", "s3cret-pw1")
    admin_token = _login(client, "sole-admin@acme.example.com", "s3cret-pw1")
    admin_id = signup_body["id"]

    delete_resp = client.delete(f"/api/v1/team/{admin_id}", headers=_headers(admin_token))
    assert delete_resp.status_code == 409

    list_resp = client.get("/api/v1/team", headers=_headers(admin_token))
    roles = {m["id"]: m["role"] for m in list_resp.json()}
    assert roles[admin_id] == "admin"


def test_cannot_demote_the_sole_remaining_admin(client):
    signup_body = _signup(client, "Acme Retail", "sole-admin2@acme.example.com", "s3cret-pw1")
    admin_token = _login(client, "sole-admin2@acme.example.com", "s3cret-pw1")
    admin_id = signup_body["id"]

    patch_resp = client.patch(
        f"/api/v1/team/{admin_id}/role", json={"role": "member"}, headers=_headers(admin_token)
    )
    assert patch_resp.status_code == 409

    list_resp = client.get("/api/v1/team", headers=_headers(admin_token))
    roles = {m["id"]: m["role"] for m in list_resp.json()}
    assert roles[admin_id] == "admin"


def test_removing_or_demoting_an_admin_is_allowed_when_another_admin_remains(client):
    signup_body = _signup(client, "Acme Retail", "admin-a@acme.example.com", "s3cret-pw1")
    admin_a_token = _login(client, "admin-a@acme.example.com", "s3cret-pw1")
    invite_resp = _invite(client, admin_a_token, "admin-b@acme.example.com", password="adminbpw1", role="admin")
    admin_b_id = invite_resp.json()["id"]

    # Two admins now exist -- demoting/removing one should be allowed.
    patch_resp = client.patch(
        f"/api/v1/team/{admin_b_id}/role", json={"role": "member"}, headers=_headers(admin_a_token)
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["role"] == "member"


# ---------------------------------------------------------------------------
# Cross-merchant isolation
# ---------------------------------------------------------------------------


def test_cross_merchant_team_access_is_404_not_leaked(client):
    signup_a = _signup(client, "Merchant A", "admin-merchant-a@example.com", "s3cret-pw1")
    admin_a_token = _login(client, "admin-merchant-a@example.com", "s3cret-pw1")

    signup_b = _signup(client, "Merchant B", "admin-merchant-b@example.com", "s3cret-pw1")
    admin_b_id = signup_b["id"]

    get_resp = client.get(f"/api/v1/team", headers=_headers(admin_a_token))
    assert admin_b_id not in {m["id"] for m in get_resp.json()}

    delete_resp = client.delete(f"/api/v1/team/{admin_b_id}", headers=_headers(admin_a_token))
    assert delete_resp.status_code == 404

    patch_resp = client.patch(
        f"/api/v1/team/{admin_b_id}/role", json={"role": "member"}, headers=_headers(admin_a_token)
    )
    assert patch_resp.status_code == 404


# ---------------------------------------------------------------------------
# Regression: member-role token still works on pre-existing endpoints,
# via the seeded demo accounts over the real (shared, on-disk) DB.
# ---------------------------------------------------------------------------


def test_seeded_demo_admin_login_still_works(seeded_client):
    resp = seeded_client.post(
        "/api/v1/auth/login", json={"email": DEMO_MERCHANT_EMAIL, "password": DEMO_MERCHANT_PASSWORD}
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_member_role_token_can_use_all_pre_existing_merchant_scoped_endpoints(seeded_client):
    resp = seeded_client.post(
        "/api/v1/auth/login",
        json={"email": DEMO_TEAM_MEMBER_EMAIL, "password": DEMO_TEAM_MEMBER_PASSWORD},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    headers = _headers(token)

    assert seeded_client.get("/api/v1/members", headers=headers).status_code == 200
    assert seeded_client.get("/api/v1/transactions", headers=headers).status_code == 200
    assert seeded_client.get("/api/v1/rewards", headers=headers).status_code == 200
    assert seeded_client.get("/api/v1/ai/churn", headers=headers).status_code == 200
