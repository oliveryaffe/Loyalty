"""A/B testing for reward structures (PLAN_BATCH3.md §5): create + bulk
random assignment, assignment stability across re-fetches, recording
redemptions against a variant, the results-comparison view's per-variant
aggregates (and that non-assigned members never inflate them), the
recommend_for_member steering filter, and ending an experiment.
"""
from __future__ import annotations

import pytest

from app.db.models import ExperimentAssignment, Member, RewardExperiment
from app.services.experiments import assign_variant


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/signup",
        json={
            "business_name": "Acme Retail",
            "email": "experiments-owner@acme.example.com",
            "password": "s3cret-pw",
        },
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "experiments-owner@acme.example.com", "password": "s3cret-pw"},
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _merchant_id(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["merchant_id"]


def _create_reward(client, headers, name="Reward", points_cost=100) -> str:
    resp = client.post(
        "/api/v1/rewards",
        json={"name": name, "points_cost": points_cost, "tier_required": "bronze"},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _make_member(db_session, merchant_id, email, points_balance=1000, tier="bronze") -> Member:
    member = Member(
        merchant_id=merchant_id,
        first_name="Test",
        last_name="Member",
        email=email,
        points_balance=points_balance,
        tier=tier,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


def _make_members(db_session, merchant_id, count, prefix="member") -> list[Member]:
    return [
        _make_member(db_session, merchant_id, email=f"{prefix}{i}@example.com")
        for i in range(count)
    ]


def _create_experiment(client, headers, variant_a_reward_id, variant_b_reward_id, traffic_split=0.5, name="Test Experiment"):
    resp = client.post(
        "/api/v1/experiments",
        json={
            "name": name,
            "variant_a_reward_id": variant_a_reward_id,
            "variant_b_reward_id": variant_b_reward_id,
            "traffic_split": traffic_split,
        },
        headers=headers,
    )
    return resp


# ---------------------------------------------------------------------------
# Create + bulk random assignment
# ---------------------------------------------------------------------------


def test_create_experiment_bulk_assigns_every_active_member(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_a = _create_reward(client, admin_headers, name="Free Coffee")
    reward_b = _create_reward(client, admin_headers, name="Free Pastry")
    members = _make_members(db_session, merchant_id, 200)

    resp = _create_experiment(client, admin_headers, reward_a, reward_b, traffic_split=0.5)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "running"
    assert body["variant_a_reward_id"] == reward_a
    assert body["variant_b_reward_id"] == reward_b

    total_assigned = body["members_assigned_a"] + body["members_assigned_b"]
    assert total_assigned == len(members)

    # Deterministic hash-based split, not perfectly 50/50, but should not be
    # wildly skewed for a 0.5 traffic_split (PLAN_BATCH3.md §5 acceptance
    # criterion 1's 45-55% tolerance example, applied loosely here since our
    # sample is smaller than the 620-member seed).
    fraction_b = body["members_assigned_b"] / total_assigned
    assert 0.30 <= fraction_b <= 0.70

    # Every member actually has exactly one assignment row.
    assignment_count = (
        db_session.query(ExperimentAssignment)
        .filter(ExperimentAssignment.experiment_id == body["id"])
        .count()
    )
    assert assignment_count == len(members)


def test_create_experiment_rejects_identical_reward_ids(client, admin_headers):
    reward_id = _create_reward(client, admin_headers)
    resp = _create_experiment(client, admin_headers, reward_id, reward_id)
    assert resp.status_code == 400


def test_create_experiment_rejects_reward_not_belonging_to_merchant(client, admin_headers):
    reward_a = _create_reward(client, admin_headers)
    resp = _create_experiment(client, admin_headers, reward_a, "does-not-exist")
    assert resp.status_code == 400


def test_create_experiment_rejects_inactive_reward(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_a = _create_reward(client, admin_headers, name="Active Reward")
    reward_b = _create_reward(client, admin_headers, name="Inactive Reward")

    from app.db.models import RewardCatalogItem

    inactive = db_session.query(RewardCatalogItem).filter(RewardCatalogItem.id == reward_b).first()
    inactive.active = False
    db_session.commit()

    resp = _create_experiment(client, admin_headers, reward_a, reward_b)
    assert resp.status_code == 400


def test_create_experiment_requires_admin(client, admin_headers):
    reward_a = _create_reward(client, admin_headers)
    reward_b = _create_reward(client, admin_headers, name="Other")

    client.post(
        "/api/v1/team/invite",
        json={"email": "experiments-teammate@acme.example.com", "password": "teammate-pw1", "role": "member"},
        headers=admin_headers,
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "experiments-teammate@acme.example.com", "password": "teammate-pw1"},
    )
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = _create_experiment(client, member_headers, reward_a, reward_b)
    assert resp.status_code == 403

    # GET endpoints stay open to any team member.
    assert client.get("/api/v1/experiments", headers=member_headers).status_code == 200


# ---------------------------------------------------------------------------
# Assignment stability across re-fetches
# ---------------------------------------------------------------------------


def test_assign_variant_is_deterministic_pure_function():
    """Same (experiment_id, member_id) always hashes to the same arm --
    the underlying mechanism that makes "don't flip a member between
    requests" possible without persisting an RNG seed."""
    first = assign_variant("exp-1", "member-1", 0.5)
    second = assign_variant("exp-1", "member-1", 0.5)
    assert first == second

    # A different member can land in a different (or the same) arm -- just
    # confirm the function is at least sometimes different across members.
    variants = {assign_variant("exp-1", f"member-{i}", 0.5) for i in range(50)}
    assert variants == {"a", "b"}


def test_refetching_experiment_does_not_reassign_members(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_a = _create_reward(client, admin_headers, name="Reward A")
    reward_b = _create_reward(client, admin_headers, name="Reward B")
    members = _make_members(db_session, merchant_id, 30)

    created = _create_experiment(client, admin_headers, reward_a, reward_b).json()
    experiment_id = created["id"]

    member = members[0]
    original_variant = (
        db_session.query(ExperimentAssignment.variant)
        .filter(ExperimentAssignment.experiment_id == experiment_id, ExperimentAssignment.member_id == member.id)
        .scalar()
    )
    assert original_variant in ("a", "b")

    # Re-fetch the experiment (detail + results) multiple times -- no
    # re-assignment endpoint exists, and the assignment row must be
    # unchanged/not duplicated.
    for _ in range(3):
        detail = client.get(f"/api/v1/experiments/{experiment_id}", headers=admin_headers).json()
        assert detail["members_assigned_a"] + detail["members_assigned_b"] == len(members)
        client.get(f"/api/v1/experiments/{experiment_id}/results", headers=admin_headers)

    refetched_variant = (
        db_session.query(ExperimentAssignment.variant)
        .filter(ExperimentAssignment.experiment_id == experiment_id, ExperimentAssignment.member_id == member.id)
        .scalar()
    )
    assert refetched_variant == original_variant

    row_count = (
        db_session.query(ExperimentAssignment)
        .filter(ExperimentAssignment.experiment_id == experiment_id, ExperimentAssignment.member_id == member.id)
        .count()
    )
    assert row_count == 1


# ---------------------------------------------------------------------------
# Recording an outcome/redemption against a variant + results aggregates
# ---------------------------------------------------------------------------


def _variant_members(db_session, experiment_id, variant) -> list[str]:
    rows = (
        db_session.query(ExperimentAssignment.member_id)
        .filter(ExperimentAssignment.experiment_id == experiment_id, ExperimentAssignment.variant == variant)
        .all()
    )
    return [r[0] for r in rows]


def test_results_view_reports_correct_per_variant_aggregates(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_a = _create_reward(client, admin_headers, name="Reward A", points_cost=100)
    reward_b = _create_reward(client, admin_headers, name="Reward B", points_cost=200)
    members = _make_members(db_session, merchant_id, 60)

    created = _create_experiment(client, admin_headers, reward_a, reward_b).json()
    experiment_id = created["id"]

    variant_a_members = _variant_members(db_session, experiment_id, "a")
    variant_b_members = _variant_members(db_session, experiment_id, "b")
    assert variant_a_members and variant_b_members  # sanity: both arms non-empty at n=60

    # Two members in variant A redeem reward A; one member in variant B
    # redeems reward B.
    for member_id in variant_a_members[:2]:
        resp = client.post(
            "/api/v1/rewards/redeem",
            json={"member_id": member_id, "reward_id": reward_a},
            headers=admin_headers,
        )
        assert resp.status_code == 200
    resp = client.post(
        "/api/v1/rewards/redeem",
        json={"member_id": variant_b_members[0], "reward_id": reward_b},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    results = client.get(f"/api/v1/experiments/{experiment_id}/results", headers=admin_headers).json()

    assert results["variant_a"]["members_assigned"] == len(variant_a_members)
    assert results["variant_a"]["redemptions_count"] == 2
    assert results["variant_a"]["total_points_spent"] == 200  # 2 * 100
    assert results["variant_a"]["redemption_rate"] == pytest.approx(2 / len(variant_a_members), abs=1e-4)

    assert results["variant_b"]["members_assigned"] == len(variant_b_members)
    assert results["variant_b"]["redemptions_count"] == 1
    assert results["variant_b"]["total_points_spent"] == 200  # 1 * 200
    assert results["variant_b"]["redemption_rate"] == pytest.approx(1 / len(variant_b_members), abs=1e-4)

    assert results["sample_size_caveat"]
    assert results["directional_winner"] in ("a", "b", "inconclusive")


def test_results_view_ignores_redemptions_from_non_assigned_members(client, db_session, admin_headers):
    """PLAN_BATCH3.md §5 acceptance criterion 4: a control test -- a member
    NOT assigned to the experiment redeeming the same catalog reward must
    not inflate either variant's count. Members created *after* the
    experiment are never auto-assigned (explicit MVP limitation), which is
    exactly the mechanism this test exercises."""
    merchant_id = _merchant_id(client, admin_headers)
    reward_a = _create_reward(client, admin_headers, name="Reward A", points_cost=100)
    reward_b = _create_reward(client, admin_headers, name="Reward B", points_cost=100)
    _make_members(db_session, merchant_id, 20)

    created = _create_experiment(client, admin_headers, reward_a, reward_b).json()
    experiment_id = created["id"]

    before = client.get(f"/api/v1/experiments/{experiment_id}/results", headers=admin_headers).json()

    # A brand-new member, created after the experiment, is never assigned --
    # redeeming variant A's reward must not count.
    outsider = _make_member(db_session, merchant_id, email="outsider@example.com")
    resp = client.post(
        "/api/v1/rewards/redeem",
        json={"member_id": outsider.id, "reward_id": reward_a},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    after = client.get(f"/api/v1/experiments/{experiment_id}/results", headers=admin_headers).json()
    assert after["variant_a"]["redemptions_count"] == before["variant_a"]["redemptions_count"]
    assert after["variant_a"]["members_assigned"] == before["variant_a"]["members_assigned"]


# ---------------------------------------------------------------------------
# recommend_for_member steering filter
# ---------------------------------------------------------------------------


def test_recommend_for_member_filters_other_variant_while_running(client, db_session, admin_headers):
    from app.ai.recommender import recommend_for_member

    merchant_id = _merchant_id(client, admin_headers)
    reward_a = _create_reward(client, admin_headers, name="Reward A", points_cost=50)
    reward_b = _create_reward(client, admin_headers, name="Reward B", points_cost=50)
    members = _make_members(db_session, merchant_id, 40, prefix="rec")

    created = _create_experiment(client, admin_headers, reward_a, reward_b).json()
    experiment_id = created["id"]

    variant_a_members = _variant_members(db_session, experiment_id, "a")
    variant_b_members = _variant_members(db_session, experiment_id, "b")
    assert variant_a_members and variant_b_members

    member_a = db_session.get(Member, variant_a_members[0])
    member_b = db_session.get(Member, variant_b_members[0])

    ranked_for_a = recommend_for_member(db_session, member_a, top_n=10)
    reward_ids_for_a = {rs.reward.id for rs in ranked_for_a}
    assert reward_a in reward_ids_for_a
    assert reward_b not in reward_ids_for_a

    ranked_for_b = recommend_for_member(db_session, member_b, top_n=10)
    reward_ids_for_b = {rs.reward.id for rs in ranked_for_b}
    assert reward_b in reward_ids_for_b
    assert reward_a not in reward_ids_for_b

    # Ending the experiment stops the filtering -- both variants can appear
    # again for either member.
    end_resp = client.post(f"/api/v1/experiments/{experiment_id}/end", headers=admin_headers)
    assert end_resp.status_code == 200
    assert end_resp.json()["status"] == "completed"

    ranked_for_a_after_end = recommend_for_member(db_session, member_a, top_n=10)
    reward_ids_after_end = {rs.reward.id for rs in ranked_for_a_after_end}
    assert reward_a in reward_ids_after_end
    assert reward_b in reward_ids_after_end


def test_end_experiment_requires_admin(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_a = _create_reward(client, admin_headers, name="Reward A")
    reward_b = _create_reward(client, admin_headers, name="Reward B")
    _make_members(db_session, merchant_id, 5)
    created = _create_experiment(client, admin_headers, reward_a, reward_b).json()

    client.post(
        "/api/v1/team/invite",
        json={"email": "experiments-end-teammate@acme.example.com", "password": "teammate-pw1", "role": "member"},
        headers=admin_headers,
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "experiments-end-teammate@acme.example.com", "password": "teammate-pw1"},
    )
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post(f"/api/v1/experiments/{created['id']}/end", headers=member_headers)
    assert resp.status_code == 403


def test_get_experiment_404_for_unknown_id(client, admin_headers):
    resp = client.get("/api/v1/experiments/does-not-exist", headers=admin_headers)
    assert resp.status_code == 404
    resp2 = client.get("/api/v1/experiments/does-not-exist/results", headers=admin_headers)
    assert resp2.status_code == 404


def test_list_experiments_scoped_to_merchant(client, db_session, admin_headers):
    merchant_id = _merchant_id(client, admin_headers)
    reward_a = _create_reward(client, admin_headers, name="Reward A")
    reward_b = _create_reward(client, admin_headers, name="Reward B")
    _make_members(db_session, merchant_id, 5)
    _create_experiment(client, admin_headers, reward_a, reward_b, name="Experiment One")

    listing = client.get("/api/v1/experiments", headers=admin_headers).json()
    assert len(listing) == 1
    assert listing[0]["name"] == "Experiment One"
    assert listing[0]["status"] == "running"


# ---------------------------------------------------------------------------
# traffic_split server-side validation (TEST_REPORT_BATCH3.md §6, MEDIUM-LOW)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_split", [-0.5, 5.0, -0.01, 1.01, 100.0])
def test_create_experiment_rejects_out_of_range_traffic_split(client, admin_headers, bad_split):
    """Previously only the frontend slider ([0.05, 0.95]) prevented an
    out-of-range traffic_split; a direct API call accepted -0.5 or 5.0 with
    a 201 and silently produced a fully-lopsided 100/0 split. Now
    server-side `Field(ge=0.0, le=1.0)` rejects it with a 422."""
    reward_a = _create_reward(client, admin_headers, name="Reward A")
    reward_b = _create_reward(client, admin_headers, name="Reward B")

    resp = _create_experiment(client, admin_headers, reward_a, reward_b, traffic_split=bad_split)
    assert resp.status_code == 422


@pytest.mark.parametrize("good_split", [0.0, 0.5, 1.0])
def test_create_experiment_accepts_boundary_traffic_split_values(client, db_session, admin_headers, good_split):
    merchant_id = _merchant_id(client, admin_headers)
    reward_a = _create_reward(client, admin_headers, name="Reward A")
    reward_b = _create_reward(client, admin_headers, name="Reward B")
    _make_members(db_session, merchant_id, 5)

    resp = _create_experiment(client, admin_headers, reward_a, reward_b, traffic_split=good_split)
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# end_experiment idempotency (TEST_REPORT_BATCH3.md §6, LOW)
# ---------------------------------------------------------------------------


def test_end_experiment_is_idempotent_ended_at_pinned_to_first_call(client, db_session, admin_headers):
    """Calling end twice must be a safe no-op the second time -- status
    stays "completed" and `ended_at` must NOT silently advance to the
    second call's timestamp."""
    merchant_id = _merchant_id(client, admin_headers)
    reward_a = _create_reward(client, admin_headers, name="Reward A")
    reward_b = _create_reward(client, admin_headers, name="Reward B")
    _make_members(db_session, merchant_id, 5)
    created = _create_experiment(client, admin_headers, reward_a, reward_b).json()
    experiment_id = created["id"]

    first = client.post(f"/api/v1/experiments/{experiment_id}/end", headers=admin_headers)
    assert first.status_code == 200
    assert first.json()["status"] == "completed"
    first_ended_at = first.json()["ended_at"]
    assert first_ended_at is not None

    second = client.post(f"/api/v1/experiments/{experiment_id}/end", headers=admin_headers)
    assert second.status_code == 200
    assert second.json()["status"] == "completed"
    assert second.json()["ended_at"] == first_ended_at

    # A third call for good measure -- still a clean no-op.
    third = client.post(f"/api/v1/experiments/{experiment_id}/end", headers=admin_headers)
    assert third.status_code == 200
    assert third.json()["ended_at"] == first_ended_at
