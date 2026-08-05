"""Cross-merchant vertical benchmarking (competitive-brief backlog item
#3) -- see app/services/benchmarking.py for the metric definition and the
"insufficient peer data" fallback rationale.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Member, Transaction, TransactionType


def _signup_and_login(client, business_name: str, email: str) -> dict:
    client.post(
        "/api/v1/auth/signup", json={"business_name": business_name, "email": email, "password": "s3cret-pw"}
    )
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": "s3cret-pw"})
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def _set_business_type(client, headers, business_type: str) -> None:
    resp = client.patch("/api/v1/settings/business-profile", json={"business_type": business_type}, headers=headers)
    assert resp.status_code == 200


def _seed_members_with_repeat_rate(db_session, merchant_id: str, total: int, repeat_count: int, prefix: str) -> None:
    """Creates `total` real (non-sample) members; the first `repeat_count`
    get 2 EARN transactions each (a "repeat visit"), the rest get exactly
    one (or zero)."""
    now = datetime.now(timezone.utc)
    for i in range(total):
        member = Member(
            merchant_id=merchant_id,
            first_name=f"{prefix}{i}",
            last_name="Member",
            email=f"{prefix.lower()}{i}@example.com",
            points_balance=0,
            tier="bronze",
        )
        db_session.add(member)
        db_session.flush()

        n_txns = 2 if i < repeat_count else 1
        for j in range(n_txns):
            db_session.add(
                Transaction(
                    member_id=member.id,
                    type=TransactionType.EARN.value,
                    amount_gbp=10.0,
                    points=10,
                    created_at=now - timedelta(days=j),
                )
            )
    db_session.commit()


@pytest.fixture()
def admin_headers(client):
    return _signup_and_login(client, "Benchmark Test Co", "benchmark-owner@acme.example.com")


def test_benchmark_unavailable_without_business_type(client, admin_headers):
    resp = client.get("/api/v1/benchmark/repeat-visit-rate", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "business type" in body["message"].lower()


def test_benchmark_unavailable_without_real_data(client, admin_headers):
    _set_business_type(client, admin_headers, "coffee_shop")
    resp = client.get("/api/v1/benchmark/repeat-visit-rate", headers=admin_headers)
    body = resp.json()
    assert body["available"] is False
    assert "sample data" in body["message"].lower() or "not enough" in body["message"].lower()


def test_benchmark_unavailable_with_insufficient_peers(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    _set_business_type(client, admin_headers, "barber_salon")
    _seed_members_with_repeat_rate(db_session, merchant_id, total=10, repeat_count=5, prefix="Solo")

    resp = client.get("/api/v1/benchmark/repeat-visit-rate", headers=admin_headers)
    body = resp.json()
    assert body["available"] is False
    assert body["your_repeat_visit_rate"] == pytest.approx(0.5)
    assert "not enough" in body["message"].lower()


def test_benchmark_available_with_enough_peers_and_computes_percentile(client, db_session):
    """4 merchants total, all 'retail', with distinct repeat-visit rates:
    self=80%, peers=60%/40%/20%. Self has the best rate in the pool, so
    should land at rank 1 of 4 -> top 25%."""
    self_headers = _signup_and_login(client, "Self Retail Co", "bench-self@acme.example.com")
    self_id = _merchant_id(client, self_headers)
    _set_business_type(client, self_headers, "retail")
    _seed_members_with_repeat_rate(db_session, self_id, total=10, repeat_count=8, prefix="Self")

    peer_rates = [0.6, 0.4, 0.2]
    for idx, rate in enumerate(peer_rates):
        peer_headers = _signup_and_login(client, f"Peer Retail Co {idx}", f"bench-peer{idx}@acme.example.com")
        peer_id = _merchant_id(client, peer_headers)
        _set_business_type(client, peer_headers, "retail")
        _seed_members_with_repeat_rate(
            db_session, peer_id, total=10, repeat_count=int(rate * 10), prefix=f"Peer{idx}"
        )

    resp = client.get("/api/v1/benchmark/repeat-visit-rate", headers=self_headers)
    body = resp.json()
    assert body["available"] is True
    assert body["peer_count"] == 3
    assert body["your_repeat_visit_rate"] == pytest.approx(0.8)
    assert body["top_percent"] == 25
    assert "retail" in body["message"].lower()
    assert "80%" in body["message"] or "80.0%" in body["message"]


def test_benchmark_excludes_sample_data_only_peers(client, db_session):
    """A peer merchant that only has sample data must not count toward
    peer_count or MIN_PEER_MERCHANTS -- comparing against Ledgerly's own
    synthetic demo data would be circular and misleading."""
    self_headers = _signup_and_login(client, "Self Salon Co", "bench-self-salon@acme.example.com")
    self_id = _merchant_id(client, self_headers)
    _set_business_type(client, self_headers, "barber_salon")
    _seed_members_with_repeat_rate(db_session, self_id, total=10, repeat_count=5, prefix="SelfSalon")

    # Two real peers (meets the threshold of 3 only if the sample-only one
    # also counted -- it must not).
    for idx in range(2):
        peer_headers = _signup_and_login(
            client, f"Peer Salon Co {idx}", f"bench-salon-peer{idx}@acme.example.com"
        )
        peer_id = _merchant_id(client, peer_headers)
        _set_business_type(client, peer_headers, "barber_salon")
        _seed_members_with_repeat_rate(db_session, peer_id, total=10, repeat_count=3, prefix=f"SalonPeer{idx}")

    # Sample-data-only merchant.
    sample_headers = _signup_and_login(client, "Sample Salon Co", "bench-salon-sample@acme.example.com")
    client.patch(
        "/api/v1/settings/business-profile", json={"business_type": "barber_salon"}, headers=sample_headers
    )
    sample_resp = client.post("/api/v1/insights/sample-data", json={"business_type": "barber_salon"}, headers=sample_headers)
    assert sample_resp.status_code == 200

    resp = client.get("/api/v1/benchmark/repeat-visit-rate", headers=self_headers)
    body = resp.json()
    assert body["available"] is False
    assert body["peer_count"] == 2  # sample-only merchant excluded


def test_benchmark_accessible_to_non_admin_team_member(client, db_session, admin_headers):
    _set_business_type(client, admin_headers, "restaurant")
    client.post(
        "/api/v1/team/invite",
        json={"email": "benchmark-teammate@acme.example.com", "password": "teammate-pw1", "role": "member"},
        headers=admin_headers,
    )
    login = client.post(
        "/api/v1/auth/login", json={"email": "benchmark-teammate@acme.example.com", "password": "teammate-pw1"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.get("/api/v1/benchmark/repeat-visit-rate", headers=member_headers)
    assert resp.status_code == 200
