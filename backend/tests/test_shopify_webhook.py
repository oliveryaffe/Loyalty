"""Shopify-style webhook ingestion: HMAC verification, idempotency,
member auto-creation, malformed-payload handling.

Reuses the same fixture JSON the demo script
(scripts/send_sample_shopify_webhook.py) sends, per PLAN_BATCH1.md.
"""
import base64
import hashlib
import hmac
import json
from math import floor
from pathlib import Path

import pytest

from app.config import settings
from app.db.models import Member, Merchant, Transaction

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "fixtures" / "shopify_order_create_sample.json"
)
SECRET = "test-shopify-secret"
WEBHOOK_URL_TMPL = "/api/v1/webhooks/shopify/{merchant_id}/orders-create"


def _raw_fixture_bytes() -> bytes:
    return FIXTURE_PATH.read_bytes()


def _fixture_dict() -> dict:
    return json.loads(_raw_fixture_bytes())


def _sign(raw_body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()).decode(
        "utf-8"
    )


@pytest.fixture()
def merchant(db_session):
    m = Merchant(business_name="Webhook Test Co", shopify_webhook_secret=SECRET)
    db_session.add(m)
    db_session.flush()
    return m


def test_valid_hmac_creates_transaction_and_credits_member(client, db_session, merchant):
    raw_body = _raw_fixture_bytes()
    signature = _sign(raw_body)

    resp = client.post(
        WEBHOOK_URL_TMPL.format(merchant_id=merchant.id),
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": signature},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["type"] == "earn"

    fixture = _fixture_dict()
    expected_points = floor(float(fixture["total_price"]) * settings.points_per_pound)
    assert body["points"] == expected_points
    assert body["amount_gbp"] == float(fixture["total_price"])

    txn = db_session.query(Transaction).filter(Transaction.id == body["id"]).first()
    assert txn is not None
    assert txn.source == "shopify"
    assert txn.external_order_id == str(fixture["id"])

    member = db_session.query(Member).filter(Member.email == fixture["customer"]["email"]).first()
    assert member is not None
    assert member.merchant_id == merchant.id
    assert member.points_balance == expected_points
    assert member.first_name == fixture["customer"]["first_name"]


def test_duplicate_webhook_is_idempotent(client, db_session, merchant):
    raw_body = _raw_fixture_bytes()
    signature = _sign(raw_body)
    url = WEBHOOK_URL_TMPL.format(merchant_id=merchant.id)

    first = client.post(
        url, content=raw_body, headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": signature}
    )
    assert first.status_code == 201

    fixture = _fixture_dict()
    member = db_session.query(Member).filter(Member.email == fixture["customer"]["email"]).first()
    balance_after_first = member.points_balance

    second = client.post(
        url, content=raw_body, headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": signature}
    )
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate_ignored"}

    db_session.refresh(member)
    assert member.points_balance == balance_after_first

    txn_count = (
        db_session.query(Transaction)
        .filter(Transaction.external_order_id == str(fixture["id"]))
        .count()
    )
    assert txn_count == 1


def test_missing_hmac_header_is_rejected(client, db_session, merchant):
    raw_body = _raw_fixture_bytes()
    resp = client.post(
        WEBHOOK_URL_TMPL.format(merchant_id=merchant.id),
        content=raw_body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert db_session.query(Transaction).count() == 0


def test_incorrect_hmac_is_rejected(client, db_session, merchant):
    raw_body = _raw_fixture_bytes()
    bad_signature = _sign(raw_body, secret="wrong-secret")
    resp = client.post(
        WEBHOOK_URL_TMPL.format(merchant_id=merchant.id),
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": bad_signature},
    )
    assert resp.status_code == 401
    assert db_session.query(Transaction).count() == 0


def test_unknown_merchant_id_404s(client):
    raw_body = _raw_fixture_bytes()
    signature = _sign(raw_body)
    resp = client.post(
        WEBHOOK_URL_TMPL.format(merchant_id="does-not-exist"),
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": signature},
    )
    assert resp.status_code == 404


def test_malformed_json_is_rejected(client, db_session, merchant):
    raw_body = b"{not valid json"
    signature = _sign(raw_body)
    resp = client.post(
        WEBHOOK_URL_TMPL.format(merchant_id=merchant.id),
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": signature},
    )
    assert resp.status_code == 422
    assert db_session.query(Transaction).count() == 0
    assert db_session.query(Member).count() == 0


def test_payload_missing_customer_is_rejected(client, db_session, merchant):
    fixture = _fixture_dict()
    del fixture["customer"]
    raw_body = json.dumps(fixture).encode("utf-8")
    signature = _sign(raw_body)

    resp = client.post(
        WEBHOOK_URL_TMPL.format(merchant_id=merchant.id),
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": signature},
    )
    assert resp.status_code == 422
    assert db_session.query(Transaction).count() == 0
    assert db_session.query(Member).count() == 0


def test_no_authorization_header_but_valid_hmac_still_succeeds(client, merchant):
    """Confirms the endpoint is intentionally outside the JWT-protected
    group -- no Authorization header is sent at all, only a valid HMAC."""
    raw_body = _raw_fixture_bytes()
    signature = _sign(raw_body)
    resp = client.post(
        WEBHOOK_URL_TMPL.format(merchant_id=merchant.id),
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": signature},
    )
    assert resp.status_code == 201


def test_valid_jwt_but_bad_hmac_still_401s(client, merchant):
    """A valid bearer JWT is irrelevant to this endpoint -- HMAC is the
    only accepted credential."""
    signup_resp = client.post(
        "/api/v1/auth/signup",
        json={"business_name": "Some Other Co", "email": "owner@other.example.com", "password": "pw123456"},
    )
    assert signup_resp.status_code == 201
    login_resp = client.post(
        "/api/v1/auth/login", json={"email": "owner@other.example.com", "password": "pw123456"}
    )
    token = login_resp.json()["access_token"]

    raw_body = _raw_fixture_bytes()
    resp = client.post(
        WEBHOOK_URL_TMPL.format(merchant_id=merchant.id),
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            # deliberately no X-Shopify-Hmac-Sha256 header
        },
    )
    assert resp.status_code == 401
