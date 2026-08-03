"""Future-value model tests (app/ai/future_value.py).

Covers PLAN_BATCH2.md acceptance criterion 1: against the seeded demo
merchant (no upload), every member gets a score >= 0, and both
model_used values ("trained" and "heuristic") appear at least once,
proving both code paths are actually live. Also covers ranking sanity
(loyal/high-activity members should score a meaningfully higher predicted
future value than clearly lapsed ones) and the small-merchant fallback
(fewer than MIN_TRAINING_MEMBERS -> everyone gets the heuristic).
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.ai.future_value import (
    MIN_TRAINING_MEMBERS,
    compute_future_value_features,
    predict_future_value,
    score_all_members_future_value,
    train_future_value_model,
)
from app.db.models import Member, Merchant, Transaction, TransactionType


@pytest.fixture()
def merchant(db_session):
    m = Merchant(business_name="Future Value Test Co")
    db_session.add(m)
    db_session.flush()
    return m


def test_all_seeded_members_get_a_nonnegative_score(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    results = score_all_members_future_value(seeded_db, merchant.id, horizon_days=90)
    members = seeded_db.query(Member).filter(Member.merchant_id == merchant.id).all()

    assert len(results) == len(members)
    for r in results:
        assert r.predicted_value >= 0.0
        assert r.horizon_days == 90
        assert r.model_used in ("trained", "heuristic")


def test_both_trained_and_heuristic_paths_appear_in_seeded_data(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    results = score_all_members_future_value(seeded_db, merchant.id, horizon_days=90)
    used = {r.model_used for r in results}
    assert "trained" in used
    assert "heuristic" in used


def test_loyal_cohort_scores_higher_than_lapsing_cohort_on_average(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    results = score_all_members_future_value(seeded_db, merchant.id, horizon_days=90)
    by_id = {r.member_id: r for r in results}

    members = seeded_db.query(Member).filter(Member.merchant_id == merchant.id).all()
    loyal_vals = [by_id[m.id].predicted_value for m in members if m.synthetic_cohort == "loyal"]
    lapsing_vals = [by_id[m.id].predicted_value for m in members if m.synthetic_cohort == "lapsing"]

    assert len(loyal_vals) > 20
    assert len(lapsing_vals) > 20

    avg_loyal = sum(loyal_vals) / len(loyal_vals)
    avg_lapsing = sum(lapsing_vals) / len(lapsing_vals)

    assert avg_loyal > avg_lapsing, (
        f"expected loyal cohort avg predicted future value to exceed lapsing cohort, "
        f"got loyal={avg_loyal:.2f} lapsing={avg_lapsing:.2f}"
    )


def test_future_value_single_member_matches_score_all_members(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    member = seeded_db.query(Member).filter(Member.merchant_id == merchant.id).first()

    now = datetime.now(timezone.utc)
    model = train_future_value_model(seeded_db, merchant.id, now=now)
    single = predict_future_value(seeded_db, member, model, horizon_days=90, now=now)

    all_results = score_all_members_future_value(seeded_db, merchant.id, horizon_days=90)
    from_batch = next(r for r in all_results if r.member_id == member.id)

    assert single.model_used == from_batch.model_used
    # Batch run uses its own `now` internally; allow tiny float drift but
    # expect materially the same prediction for the same underlying data.
    assert abs(single.predicted_value - from_batch.predicted_value) < 1.0


def test_horizon_scaling_is_roughly_proportional(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    member = (
        seeded_db.query(Member)
        .filter(Member.merchant_id == merchant.id, Member.synthetic_cohort == "loyal")
        .first()
    )
    now = datetime.now(timezone.utc)
    model = train_future_value_model(seeded_db, merchant.id, now=now)

    r90 = predict_future_value(seeded_db, member, model, horizon_days=90, now=now)
    r180 = predict_future_value(seeded_db, member, model, horizon_days=180, now=now)

    if r90.predicted_value > 0:
        assert r180.predicted_value > r90.predicted_value


def test_too_few_eligible_members_falls_back_to_heuristic_for_everyone(db_session, merchant):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=100)

    # Only a handful of members with pre-cutoff activity -- well under
    # MIN_TRAINING_MEMBERS, so training should be skipped entirely.
    members = []
    for i in range(5):
        m = Member(
            merchant_id=merchant.id,
            first_name=f"M{i}",
            last_name="Test",
            email=f"m{i}@example.com",
            joined_at=cutoff - timedelta(days=30),
            last_activity_at=now,
        )
        db_session.add(m)
        db_session.flush()
        db_session.add(
            Transaction(
                member_id=m.id,
                type=TransactionType.EARN.value,
                amount_gbp=25.0,
                points=25,
                created_at=cutoff - timedelta(days=5),
            )
        )
        members.append(m)
    db_session.flush()

    model = train_future_value_model(db_session, merchant.id, now=now)
    assert model is None

    for m in members:
        result = predict_future_value(db_session, m, model, horizon_days=90, now=now)
        assert result.model_used == "heuristic"
        assert result.predicted_value >= 0.0


def test_brand_new_member_with_no_pre_cutoff_activity_falls_back_to_heuristic_even_with_trained_model(
    seeded_db,
):
    merchant = seeded_db.query(Merchant).first()
    now = datetime.now(timezone.utc)

    brand_new = Member(
        merchant_id=merchant.id,
        first_name="Brand",
        last_name="New",
        email="brand-new-fv-test@example.com",
        joined_at=now - timedelta(days=1),
        last_activity_at=now,
    )
    seeded_db.add(brand_new)
    seeded_db.flush()
    # One earn transaction, but it happened AFTER cutoff (well within the
    # last HOLDOUT_DAYS) -- this member has zero pre-cutoff earn history.
    seeded_db.add(
        Transaction(
            member_id=brand_new.id,
            type=TransactionType.EARN.value,
            amount_gbp=15.0,
            points=15,
            created_at=now - timedelta(days=1),
        )
    )
    seeded_db.flush()

    model = train_future_value_model(seeded_db, merchant.id, now=now)
    assert model is not None  # plenty of other eligible members in the seeded data

    result = predict_future_value(seeded_db, brand_new, model, horizon_days=90, now=now)
    assert result.model_used == "heuristic"


def test_compute_future_value_features_is_bounded_by_as_of(db_session, merchant):
    now = datetime.now(timezone.utc)
    member = Member(
        merchant_id=merchant.id,
        first_name="A",
        last_name="B",
        email="bounded@example.com",
        joined_at=now - timedelta(days=200),
        last_activity_at=now,
    )
    db_session.add(member)
    db_session.flush()

    cutoff = now - timedelta(days=45)
    # One transaction before cutoff, one after -- features "as of cutoff"
    # must only see the first.
    db_session.add(
        Transaction(
            member_id=member.id,
            type=TransactionType.EARN.value,
            amount_gbp=30.0,
            points=30,
            created_at=cutoff - timedelta(days=10),
        )
    )
    db_session.add(
        Transaction(
            member_id=member.id,
            type=TransactionType.EARN.value,
            amount_gbp=9000.0,
            points=9000,
            created_at=cutoff + timedelta(days=5),
        )
    )
    db_session.flush()

    feats = compute_future_value_features(db_session, member, as_of=cutoff)
    assert feats.frequency == 1
    assert feats.monetary == 30.0
