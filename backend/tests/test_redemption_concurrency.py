"""Regression test for the redemption TOCTOU race condition reported in
TEST_REPORT.md §c (CRITICAL): 10 concurrent redeem requests against a member
with exactly enough points for one redemption used to all return HTTP 200,
recording 10 separate Redemption/Transaction rows against a 100-point
balance.

This deliberately does NOT use the `client`/`db_session` fixtures from
conftest.py: that `client` fixture overrides `get_db` to hand every request
the *same* SQLAlchemy `Session` object, which is not thread-safe and would
either serialize everything through Python-level contention or produce
undefined behavior unrelated to real database-level concurrency -- masking
the very race we need to prove is fixed.

Instead this spins up the real app on a background thread via a live
uvicorn server, so each HTTP request gets its own request-scoped `Session`
(the normal `get_db` dependency) talking to the same on-disk SQLite test
database, and fires genuinely concurrent HTTP requests at it with a
thread pool -- the closest in-process approximation of the tester's
original repro.
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import uvicorn

from app.main import app


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


def test_concurrent_redemptions_only_one_succeeds_and_balance_is_correct(live_server):
    # `trust_env=False`: ignore any HTTP_PROXY/ALL_PROXY environment
    # variables (e.g. set by sandboxed CI environments) which would
    # otherwise route this loopback traffic through an external proxy that
    # can't reach 127.0.0.1.
    with httpx.Client(base_url=live_server, timeout=10, trust_env=False) as c:
        merchant_email = f"race-{uuid.uuid4().hex[:8]}@race-test.example.com"
        signup = c.post(
            "/api/v1/auth/signup",
            json={
                "business_name": "Race Condition Co",
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
            json={"first_name": "Race", "last_name": "Condition", "email": "racer@example.com"},
            headers=headers,
        )
        assert member_resp.status_code == 201
        member_id = member_resp.json()["id"]

        reward_resp = c.post(
            "/api/v1/rewards",
            json={"name": "Free Coffee", "points_cost": 100, "tier_required": "bronze"},
            headers=headers,
        )
        assert reward_resp.status_code == 201
        reward_id = reward_resp.json()["id"]

        # Give the member exactly enough points for ONE redemption.
        earn_resp = c.post(
            "/api/v1/transactions",
            json={"member_id": member_id, "amount_gbp": 100},
            headers=headers,
        )
        assert earn_resp.status_code == 201
        balance_before = c.get(f"/api/v1/members/{member_id}", headers=headers).json()["points_balance"]
        assert balance_before == 100

        def redeem_once(_: int) -> httpx.Response:
            return c.post(
                "/api/v1/rewards/redeem",
                json={"member_id": member_id, "reward_id": reward_id},
                headers=headers,
            )

        # Fire 10 concurrent redemption requests against the same 100-point
        # balance / 100-point reward -- only one should ever be able to
        # succeed.
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(redeem_once, range(10)))

        status_codes = [r.status_code for r in results]
        successes = [r for r in results if r.status_code == 200]
        rejections = [r for r in results if r.status_code != 200]

        assert len(successes) == 1, (
            f"expected exactly 1 of 10 concurrent redemptions to succeed, "
            f"got {len(successes)}: status codes = {status_codes}"
        )
        assert len(rejections) == 9
        assert all(400 <= code < 500 for code in (r.status_code for r in rejections)), status_codes

        final_balance = c.get(f"/api/v1/members/{member_id}", headers=headers).json()["points_balance"]
        assert final_balance == 0, f"expected final balance 0, got {final_balance}"
