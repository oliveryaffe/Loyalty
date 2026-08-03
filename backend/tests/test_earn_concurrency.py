"""Regression tests for CRITICAL-2 in TEST_REPORT_BATCH1.md:

  "earn_points() has the same class of read-modify-write race as the
  redemption bug fixed in the original pass, exposed via concurrent
  Shopify webhook replays with the same external_order_id. The tester's
  barrier-synchronized reproduction showed 2 duplicate Transaction rows
  and a corrupted final balance (49 instead of the correct 98) under
  forced concurrent interleaving."

Two things were broken and are covered by two separate tests here:

1. `earn_points()` credited `points_balance` via a Python-level
   `member.points_balance += points` read-modify-write -- a lost-update
   race, the same bug shape `redeem_reward()` was already hardened
   against. Fixed by crediting via a single atomic
   `UPDATE members SET points_balance = points_balance + :points WHERE
   id = :id` (app/services/ledger.py).

2. The Shopify webhook's idempotency check
   (`app/services/shopify.py::ingest_shopify_order`) was a plain
   SELECT-then-INSERT with no DB-level constraint backing it -- a TOCTOU
   race under concurrent redelivery of the same `external_order_id`.
   Fixed by adding a UNIQUE constraint on `Transaction.external_order_id`
   (app/db/models.py) and catching the resulting IntegrityError as the
   authoritative "already processed" signal, not just the SELECT.

Both tests drive a real live uvicorn server so each HTTP request gets its
own request-scoped DB session (the normal `get_db` dependency), same
technique as tests/test_redemption_concurrency.py -- the shared-session
`client`/`db_session` fixtures from conftest.py are not thread-safe and
would mask real database-level concurrency. A threading.Barrier is used
to force all requests to fire at (as close to) the same instant as
possible, since TEST_REPORT_BATCH1.md notes real HTTP-level timing alone
(even 20 concurrent requests across `uvicorn --workers 3`) was not
reliable enough to hit the race against the old code -- only a forced,
tightly-synchronized interleaving reproduced it.
"""
import base64
import hashlib
import hmac
import json
import threading
import time
import uuid
from math import floor
from pathlib import Path

import httpx
import pytest
import uvicorn

from app.config import settings
from app.db.base import SessionLocal
from app.db.models import Member, Merchant, Transaction
from app.main import app

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "fixtures" / "shopify_order_create_sample.json"
)
SECRET = "concurrency-test-shopify-secret"


def _sign(raw_body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()).decode(
        "utf-8"
    )


@pytest.fixture()
def live_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn test server failed to start"

    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def merchant_with_shopify_secret():
    """Create a Merchant directly against the real (shared, file-backed) DB
    that the live server's SessionLocal talks to -- same technique as
    conftest.py's seeded_db fixture. There's no HTTP endpoint to set
    shopify_webhook_secret on a freshly-signed-up merchant, so we insert it
    directly, matching how tests/test_shopify_webhook.py's `merchant`
    fixture works (just against the real engine instead of the isolated
    in-memory one, since the live server needs to see it)."""
    db = SessionLocal()
    try:
        m = Merchant(business_name="Earn Concurrency Test Co", shopify_webhook_secret=SECRET)
        db.add(m)
        db.commit()
        db.refresh(m)
        merchant_id = m.id
    finally:
        db.close()
    return merchant_id


def test_concurrent_duplicate_order_id_webhooks_produce_exactly_one_transaction(
    live_server, merchant_with_shopify_secret
):
    """The tester's exact scenario: many concurrent deliveries of a
    webhook carrying the *same* external_order_id must result in exactly
    one Transaction row and a correctly-credited (not doubled, not lost)
    points_balance -- never 2 rows / never a corrupted balance like the
    tester's reproduced `49` instead of `98`.
    """
    fixture = json.loads(FIXTURE_PATH.read_bytes())
    unique_order_id = 900_000_000_000_000_000 + (uuid.uuid4().int % 1_000_000)
    fixture["id"] = unique_order_id
    fixture["customer"]["email"] = f"concurrency-{uuid.uuid4().hex[:8]}@example.com"
    raw_body = json.dumps(fixture).encode("utf-8")
    signature = _sign(raw_body)

    url = f"{live_server}/api/v1/webhooks/shopify/{merchant_with_shopify_secret}/orders-create"
    headers = {"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": signature}

    n_requests = 25
    barrier = threading.Barrier(n_requests)
    results: list[httpx.Response | None] = [None] * n_requests

    def deliver(i: int) -> None:
        with httpx.Client(timeout=10, trust_env=False) as c:
            barrier.wait()  # force all requests to fire at (as close to) the same instant
            results[i] = c.post(url, content=raw_body, headers=headers)

    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(n_requests)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert all(r is not None for r in results), "not all concurrent webhook deliveries completed"
    status_codes = [r.status_code for r in results]  # type: ignore[union-attr]
    successes = [r for r in results if r.status_code == 201]  # type: ignore[union-attr]
    duplicates = [r for r in results if r.status_code == 200]  # type: ignore[union-attr]

    assert len(successes) == 1, (
        f"expected exactly 1 of {n_requests} concurrent identical deliveries to create a "
        f"Transaction, got {len(successes)}: status codes = {status_codes}"
    )
    assert len(duplicates) == n_requests - 1, (
        f"expected the remaining {n_requests - 1} to be duplicate_ignored: {status_codes}"
    )
    assert all(r.json() == {"status": "duplicate_ignored"} for r in duplicates)  # type: ignore[union-attr]

    expected_points = floor(float(fixture["total_price"]) * settings.points_per_pound)

    db = SessionLocal()
    try:
        txns = (
            db.query(Transaction)
            .filter(Transaction.external_order_id == str(unique_order_id))
            .all()
        )
        assert len(txns) == 1, (
            f"expected exactly 1 Transaction row for external_order_id={unique_order_id}, "
            f"found {len(txns)} -- duplicate rows mean the idempotency race is back"
        )
        assert txns[0].points == expected_points

        member = db.query(Member).filter(Member.email == fixture["customer"]["email"]).first()
        assert member is not None
        assert member.points_balance == expected_points, (
            f"expected points_balance == {expected_points} (exactly one earn applied, no "
            f"double-credit from a duplicate, no lost update), got {member.points_balance}"
        )
    finally:
        db.close()


def test_concurrent_distinct_earns_for_same_member_never_lose_an_update(live_server):
    """Isolates the earn_points() lost-update bug itself, independent of
    the webhook idempotency layer: N concurrent *distinct* earn events
    (POST /api/v1/transactions, no external_order_id involved at all) for
    the same member must all durably apply. Before the fix,
    `member.points_balance += points` let concurrent requests read the
    same stale starting balance and clobber each other's increment --
    exactly the pattern the tester's forced-interleaving repro proved
    against the webhook path (correct total 98, got 49 -- half the
    increments silently lost).
    """
    with httpx.Client(base_url=live_server, timeout=10, trust_env=False) as c:
        merchant_email = f"earn-race-{uuid.uuid4().hex[:8]}@earn-race-test.example.com"
        signup = c.post(
            "/api/v1/auth/signup",
            json={
                "business_name": "Earn Race Co",
                "email": merchant_email,
                "password": "s3cret-pw",
            },
        )
        assert signup.status_code == 201

        login = c.post(
            "/api/v1/auth/login",
            json={"email": merchant_email, "password": "s3cret-pw"},
        )
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        member_resp = c.post(
            "/api/v1/members",
            json={"first_name": "Earn", "last_name": "Race", "email": "earn-racer@example.com"},
            headers=headers,
        )
        assert member_resp.status_code == 201
        member_id = member_resp.json()["id"]

        n_requests = 20
        amount_gbp = 7  # -> 7 points per earn (points_per_pound defaults to 1)
        barrier = threading.Barrier(n_requests)
        results: list[httpx.Response | None] = [None] * n_requests

        def earn_once(i: int) -> None:
            with httpx.Client(base_url=live_server, timeout=10, trust_env=False) as tc:
                barrier.wait()
                results[i] = tc.post(
                    "/api/v1/transactions",
                    json={"member_id": member_id, "amount_gbp": amount_gbp},
                    headers=headers,
                )

        threads = [threading.Thread(target=earn_once, args=(i,)) for i in range(n_requests)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert all(r is not None for r in results), "not all concurrent earn requests completed"
        status_codes = [r.status_code for r in results]  # type: ignore[union-attr]
        assert all(code == 201 for code in status_codes), (
            f"expected all {n_requests} distinct earns to succeed: {status_codes}"
        )

        expected_points_per_earn = floor(amount_gbp * settings.points_per_pound)
        expected_total = expected_points_per_earn * n_requests

        final_balance = c.get(f"/api/v1/members/{member_id}", headers=headers).json()["points_balance"]
        assert final_balance == expected_total, (
            f"expected final balance {expected_total} ({n_requests} x {expected_points_per_earn}, "
            f"no lost updates), got {final_balance}"
        )

        txn_count = len(
            c.get(f"/api/v1/transactions?member_id={member_id}&limit=100", headers=headers).json()
        )
        assert txn_count == n_requests, f"expected {n_requests} Transaction rows, got {txn_count}"
