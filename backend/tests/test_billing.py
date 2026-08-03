"""Stripe billing (PLAN_BATCH3.md §2): checkout/portal session creation,
`GET /billing/subscription`, webhook event handling + idempotency, the
hard/soft-lock gating (`require_active_subscription`), and the exemption
list (auth, billing itself, Shopify webhook ingestion, GDPR erasure/
export must never be paywalled).

No real Stripe credentials exist in this environment -- every test that
touches the `stripe` SDK mocks the exact call sites
(`stripe.checkout.Session.create`, `stripe.billing_portal.Session.create`,
`stripe.Webhook.construct_event`) via monkeypatch, never hitting Stripe's
network. app/services/billing.py is written against the real SDK's
documented API shape so it is correct once the owner supplies real keys.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import stripe

from app.config import settings
from app.db.models import BillingEvent, Merchant

WEBHOOK_URL = "/api/v1/billing/webhook"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def stripe_configured(monkeypatch):
    """Configures all Stripe settings so billing endpoints don't 503."""
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
    monkeypatch.setattr(settings, "stripe_publishable_key", "pk_test_123")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test_123")
    monkeypatch.setattr(settings, "stripe_price_id_starter", "price_starter_123")
    monkeypatch.setattr(settings, "stripe_price_id_growth", "price_growth_123")
    monkeypatch.setattr(settings, "stripe_price_id_scale", "price_scale_123")
    yield


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={
            "business_name": "Acme Retail",
            "email": "billing-owner@acme.example.com",
            "password": "s3cret-pw",
        },
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "billing-owner@acme.example.com", "password": "s3cret-pw"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def member_role_headers(client, admin_headers):
    client.post(
        "/api/v1/team/invite",
        json={"email": "billing-teammate@acme.example.com", "password": "teammate-pw1", "role": "member"},
        headers=admin_headers,
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "billing-teammate@acme.example.com", "password": "teammate-pw1"},
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def _set_status(db_session, merchant_id: str, status: str | None) -> None:
    merchant = db_session.get(Merchant, merchant_id)
    merchant.subscription_status = status
    db_session.commit()


def _fake_checkout_create(monkeypatch, captured: dict):
    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(url="https://checkout.stripe.com/test_session_123", id="cs_test_123")

    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(fake_create))


def _fake_portal_create(monkeypatch, captured: dict):
    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(url="https://billing.stripe.com/test_portal_123", id="bps_test_123")

    monkeypatch.setattr(stripe.billing_portal.Session, "create", staticmethod(fake_create))


def _bypass_signature(monkeypatch):
    """Makes stripe.Webhook.construct_event just json.loads the raw body it
    was given -- lets tests send the event payload directly as the request
    body without needing a real signing secret / HMAC computation."""

    def fake_construct_event(payload, sig_header, secret):
        return json.loads(payload)

    monkeypatch.setattr(stripe.Webhook, "construct_event", fake_construct_event)


def _post_event(client, event: dict):
    return client.post(
        WEBHOOK_URL,
        content=json.dumps(event).encode("utf-8"),
        headers={"Content-Type": "application/json", "Stripe-Signature": "t=1,v1=fake"},
    )


# ---------------------------------------------------------------------------
# GET /billing/subscription
# ---------------------------------------------------------------------------


def test_new_signup_defaults_to_trialing_with_trial_end(client, admin_headers):
    resp = client.get("/api/v1/billing/subscription", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["subscription_status"] == "trialing"
    assert body["subscription_tier"] is None
    assert body["trial_ends_at"] is not None


def test_get_subscription_accessible_to_member_role(client, admin_headers, member_role_headers):
    resp = client.get("/api/v1/billing/subscription", headers=member_role_headers)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /billing/checkout-session
# ---------------------------------------------------------------------------


def test_checkout_session_creation_success(client, admin_headers, stripe_configured, monkeypatch):
    captured: dict = {}
    _fake_checkout_create(monkeypatch, captured)

    resp = client.post(
        "/api/v1/billing/checkout-session", json={"tier": "growth"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json() == {"checkout_url": "https://checkout.stripe.com/test_session_123"}

    assert captured["mode"] == "subscription"
    assert captured["line_items"] == [{"price": "price_growth_123", "quantity": 1}]
    assert captured["subscription_data"] == {"trial_period_days": 14}
    merchant_id = _merchant_id(client, admin_headers)
    assert captured["client_reference_id"] == merchant_id
    assert captured["customer_email"] == "billing-owner@acme.example.com"


def test_checkout_session_requires_admin_role(
    client, admin_headers, member_role_headers, stripe_configured, monkeypatch
):
    _fake_checkout_create(monkeypatch, {})
    resp = client.post(
        "/api/v1/billing/checkout-session", json={"tier": "starter"}, headers=member_role_headers
    )
    assert resp.status_code == 403


def test_checkout_session_503_when_stripe_not_configured(client, admin_headers):
    resp = client.post(
        "/api/v1/billing/checkout-session", json={"tier": "starter"}, headers=admin_headers
    )
    assert resp.status_code == 503


def test_checkout_session_503_for_unconfigured_tier_price(client, admin_headers, stripe_configured, monkeypatch):
    monkeypatch.setattr(settings, "stripe_price_id_scale", None)
    resp = client.post("/api/v1/billing/checkout-session", json={"tier": "scale"}, headers=admin_headers)
    assert resp.status_code == 503


def test_checkout_session_rejects_unknown_tier(client, admin_headers, stripe_configured):
    resp = client.post(
        "/api/v1/billing/checkout-session", json={"tier": "enterprise"}, headers=admin_headers
    )
    assert resp.status_code == 422  # pydantic Literal validation


# ---------------------------------------------------------------------------
# POST /billing/portal-session
# ---------------------------------------------------------------------------


def test_portal_session_404_when_no_customer_id_yet(client, admin_headers, stripe_configured):
    resp = client.post("/api/v1/billing/portal-session", headers=admin_headers)
    assert resp.status_code == 404


def test_portal_session_creation_success(client, db_session, admin_headers, stripe_configured, monkeypatch):
    merchant_id = _merchant_id(client, admin_headers)
    merchant = db_session.get(Merchant, merchant_id)
    merchant.stripe_customer_id = "cus_test_abc"
    db_session.commit()

    captured: dict = {}
    _fake_portal_create(monkeypatch, captured)

    resp = client.post("/api/v1/billing/portal-session", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json() == {"portal_url": "https://billing.stripe.com/test_portal_123"}
    assert captured["customer"] == "cus_test_abc"


def test_portal_session_requires_admin_role(client, admin_headers, member_role_headers, stripe_configured):
    resp = client.post("/api/v1/billing/portal-session", headers=member_role_headers)
    assert resp.status_code == 403


def test_portal_session_503_when_stripe_not_configured(client, admin_headers):
    resp = client.post("/api/v1/billing/portal-session", headers=admin_headers)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Webhook: signature verification + idempotency
# ---------------------------------------------------------------------------


def test_webhook_503_when_not_configured(client):
    resp = _post_event(client, {"id": "evt_x", "type": "invoice.paid", "data": {"object": {}}})
    assert resp.status_code == 503


def test_webhook_invalid_signature_is_401(client, stripe_configured, monkeypatch):
    def fake_construct_event(payload, sig_header, secret):
        raise stripe.SignatureVerificationError("bad signature", sig_header)

    monkeypatch.setattr(stripe.Webhook, "construct_event", fake_construct_event)

    resp = client.post(
        WEBHOOK_URL,
        content=b'{"id": "evt_bad"}',
        headers={"Content-Type": "application/json", "Stripe-Signature": "t=1,v1=bad"},
    )
    assert resp.status_code == 401


def test_webhook_idempotency_same_event_id_processed_once(
    client, db_session, admin_headers, stripe_configured, monkeypatch
):
    _bypass_signature(monkeypatch)
    merchant_id = _merchant_id(client, admin_headers)

    event = {
        "id": "evt_dup_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_dup_1",
                "object": "checkout.session",
                "customer": "cus_dup_1",
                "subscription": "sub_dup_1",
                "client_reference_id": merchant_id,
            }
        },
    }

    first = _post_event(client, event)
    assert first.status_code == 200
    assert first.json() == {"status": "processed"}

    second = _post_event(client, event)
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate_ignored"}

    rows = db_session.query(BillingEvent).filter(BillingEvent.stripe_event_id == "evt_dup_1").all()
    assert len(rows) == 1
    assert rows[0].merchant_id == merchant_id
    assert rows[0].event_type == "checkout.session.completed"


# ---------------------------------------------------------------------------
# Webhook: each event type mutates the merchant correctly
# ---------------------------------------------------------------------------


def test_webhook_checkout_session_completed_sets_customer_and_subscription_id(
    client, db_session, admin_headers, stripe_configured, monkeypatch
):
    _bypass_signature(monkeypatch)
    merchant_id = _merchant_id(client, admin_headers)

    event = {
        "id": "evt_checkout_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "object": "checkout.session",
                "customer": "cus_1",
                "subscription": "sub_1",
                "client_reference_id": merchant_id,
            }
        },
    }
    resp = _post_event(client, event)
    assert resp.status_code == 200

    merchant = db_session.get(Merchant, merchant_id)
    db_session.refresh(merchant)
    assert merchant.stripe_customer_id == "cus_1"
    assert merchant.stripe_subscription_id == "sub_1"


def test_webhook_subscription_created_sets_status_tier_and_period_end(
    client, db_session, admin_headers, stripe_configured, monkeypatch
):
    _bypass_signature(monkeypatch)
    merchant_id = _merchant_id(client, admin_headers)
    merchant = db_session.get(Merchant, merchant_id)
    merchant.stripe_customer_id = "cus_2"
    db_session.commit()

    period_end_ts = int(time.time()) + 30 * 86400
    event = {
        "id": "evt_sub_created_1",
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_2",
                "object": "subscription",
                "customer": "cus_2",
                "status": "active",
                "current_period_end": period_end_ts,
                "items": {"data": [{"price": {"id": "price_growth_123"}}]},
            }
        },
    }
    resp = _post_event(client, event)
    assert resp.status_code == 200

    db_session.refresh(merchant)
    assert merchant.stripe_subscription_id == "sub_2"
    assert merchant.subscription_status == "active"
    assert merchant.subscription_tier == "growth"
    assert merchant.subscription_current_period_end is not None
    # SQLite drops tzinfo on round-trip (the stored wall-clock value is
    # still UTC, per app/services/billing.py::_to_datetime) -- reattach UTC
    # before comparing so this assertion is correct regardless of the host
    # machine's local timezone.
    stored = merchant.subscription_current_period_end
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert abs(stored.timestamp() - period_end_ts) < 2


def test_webhook_subscription_updated_changes_status(
    client, db_session, admin_headers, stripe_configured, monkeypatch
):
    _bypass_signature(monkeypatch)
    merchant_id = _merchant_id(client, admin_headers)
    merchant = db_session.get(Merchant, merchant_id)
    merchant.stripe_customer_id = "cus_3"
    merchant.stripe_subscription_id = "sub_3"
    merchant.subscription_status = "active"
    db_session.commit()

    event = {
        "id": "evt_sub_updated_1",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_3",
                "object": "subscription",
                "customer": "cus_3",
                "status": "past_due",
                "current_period_end": int(time.time()) + 86400,
                "items": {"data": [{"price": {"id": "price_growth_123"}}]},
            }
        },
    }
    resp = _post_event(client, event)
    assert resp.status_code == 200

    db_session.refresh(merchant)
    assert merchant.subscription_status == "past_due"


def test_webhook_subscription_deleted_sets_canceled(
    client, db_session, admin_headers, stripe_configured, monkeypatch
):
    _bypass_signature(monkeypatch)
    merchant_id = _merchant_id(client, admin_headers)
    merchant = db_session.get(Merchant, merchant_id)
    merchant.stripe_customer_id = "cus_4"
    merchant.stripe_subscription_id = "sub_4"
    merchant.subscription_status = "active"
    db_session.commit()

    event = {
        "id": "evt_sub_deleted_1",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": "sub_4",
                "object": "subscription",
                "customer": "cus_4",
                "status": "canceled",
            }
        },
    }
    resp = _post_event(client, event)
    assert resp.status_code == 200

    db_session.refresh(merchant)
    assert merchant.subscription_status == "canceled"


def test_webhook_invoice_payment_failed_sets_past_due(
    client, db_session, admin_headers, stripe_configured, monkeypatch
):
    _bypass_signature(monkeypatch)
    merchant_id = _merchant_id(client, admin_headers)
    merchant = db_session.get(Merchant, merchant_id)
    merchant.stripe_customer_id = "cus_5"
    merchant.stripe_subscription_id = "sub_5"
    merchant.subscription_status = "active"
    db_session.commit()

    event = {
        "id": "evt_invoice_failed_1",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "id": "in_1",
                "object": "invoice",
                "customer": "cus_5",
                "subscription": "sub_5",
            }
        },
    }
    resp = _post_event(client, event)
    assert resp.status_code == 200

    db_session.refresh(merchant)
    assert merchant.subscription_status == "past_due"


def test_webhook_invoice_paid_sets_active(client, db_session, admin_headers, stripe_configured, monkeypatch):
    _bypass_signature(monkeypatch)
    merchant_id = _merchant_id(client, admin_headers)
    merchant = db_session.get(Merchant, merchant_id)
    merchant.stripe_customer_id = "cus_6"
    merchant.stripe_subscription_id = "sub_6"
    merchant.subscription_status = "past_due"
    db_session.commit()

    event = {
        "id": "evt_invoice_paid_1",
        "type": "invoice.paid",
        "data": {
            "object": {
                "id": "in_2",
                "object": "invoice",
                "customer": "cus_6",
                "subscription": "sub_6",
            }
        },
    }
    resp = _post_event(client, event)
    assert resp.status_code == 200

    db_session.refresh(merchant)
    assert merchant.subscription_status == "active"


def test_webhook_unrecognized_event_type_is_a_no_op_200(client, stripe_configured, monkeypatch):
    _bypass_signature(monkeypatch)
    event = {"id": "evt_unknown_1", "type": "customer.updated", "data": {"object": {}}}
    resp = _post_event(client, event)
    assert resp.status_code == 200
    assert resp.json() == {"status": "processed"}


# ---------------------------------------------------------------------------
# Hard lock / soft lock gating on a representative protected route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_value", ["canceled", "unpaid", "incomplete", "incomplete_expired", None])
def test_hard_locked_statuses_get_402_on_members_route(client, db_session, admin_headers, status_value):
    merchant_id = _merchant_id(client, admin_headers)
    _set_status(db_session, merchant_id, status_value)

    resp = client.get("/api/v1/members", headers=admin_headers)
    assert resp.status_code == 402


def test_past_due_is_soft_lock_still_200_on_members_route(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    _set_status(db_session, merchant_id, "past_due")

    resp = client.get("/api/v1/members", headers=admin_headers)
    assert resp.status_code == 200


@pytest.mark.parametrize("status_value", ["active", "trialing"])
def test_allowed_statuses_get_200_on_members_route(client, db_session, admin_headers, status_value):
    merchant_id = _merchant_id(client, admin_headers)
    _set_status(db_session, merchant_id, status_value)

    resp = client.get("/api/v1/members", headers=admin_headers)
    assert resp.status_code == 200


def test_hard_locked_merchant_402_on_transactions_rewards_insights_ai(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    _set_status(db_session, merchant_id, "canceled")

    assert client.get("/api/v1/transactions", headers=admin_headers).status_code == 402
    assert client.get("/api/v1/rewards", headers=admin_headers).status_code == 402
    assert client.get("/api/v1/ai/churn", headers=admin_headers).status_code == 402
    assert client.get("/api/v1/insights/future-value", headers=admin_headers).status_code == 402
    assert (
        client.post(
            "/api/v1/members", json={"first_name": "A", "last_name": "B", "email": "ab@example.com"}, headers=admin_headers
        ).status_code
        == 402
    )


# ---------------------------------------------------------------------------
# Exemption list: auth / billing / webhooks / GDPR must never be paywalled
# ---------------------------------------------------------------------------


def test_hard_locked_merchant_can_still_use_auth_me(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    _set_status(db_session, merchant_id, "canceled")

    resp = client.get("/api/v1/auth/me", headers=admin_headers)
    assert resp.status_code == 200


def test_hard_locked_merchant_can_still_login(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    _set_status(db_session, merchant_id, "unpaid")

    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "billing-owner@acme.example.com", "password": "s3cret-pw"},
    )
    assert resp.status_code == 200


def test_hard_locked_merchant_can_still_reach_billing_endpoints(
    client, db_session, admin_headers, stripe_configured, monkeypatch
):
    merchant_id = _merchant_id(client, admin_headers)
    _set_status(db_session, merchant_id, "canceled")

    assert client.get("/api/v1/billing/subscription", headers=admin_headers).status_code == 200

    captured: dict = {}
    _fake_checkout_create(monkeypatch, captured)
    resp = client.post("/api/v1/billing/checkout-session", json={"tier": "starter"}, headers=admin_headers)
    assert resp.status_code == 200


def test_hard_locked_merchant_can_still_call_gdpr_erase_and_export(client, db_session, admin_headers):
    # Create the member *before* locking the merchant, since member creation
    # itself goes through the now-gated POST /members endpoint.
    create_resp = client.post(
        "/api/v1/members",
        json={"first_name": "Grace", "last_name": "Hopper", "email": "grace-billing@example.com"},
        headers=admin_headers,
    )
    assert create_resp.status_code == 201
    member_id = create_resp.json()["id"]

    merchant_id = _merchant_id(client, admin_headers)
    _set_status(db_session, merchant_id, "canceled")

    export_resp = client.get(f"/api/v1/members/{member_id}/gdpr-export", headers=admin_headers)
    assert export_resp.status_code == 200

    erase_resp = client.post(f"/api/v1/members/{member_id}/gdpr-erase", headers=admin_headers)
    assert erase_resp.status_code == 200
    assert erase_resp.json()["already_erased"] is False


def test_hard_locked_merchant_shopify_webhook_ingestion_still_works(client, db_session, admin_headers):
    import base64
    import hashlib
    import hmac
    from pathlib import Path

    merchant_id = _merchant_id(client, admin_headers)
    merchant = db_session.get(Merchant, merchant_id)
    secret = "billing-test-shopify-secret"
    merchant.shopify_webhook_secret = secret
    merchant.subscription_status = "canceled"
    db_session.commit()

    fixture_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "fixtures" / "shopify_order_create_sample.json"
    )
    raw_body = fixture_path.read_bytes()
    signature = base64.b64encode(hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()).decode(
        "utf-8"
    )

    resp = client.post(
        f"/api/v1/webhooks/shopify/{merchant_id}/orders-create",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": signature},
    )
    assert resp.status_code == 201
