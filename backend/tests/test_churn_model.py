"""Churn risk scoring tests (app/ai/churn_model.py).

Covers both the pure scoring function (edge cases) and, against the real
seeded synthetic dataset, the acceptance criterion that a "lapsing" cohort
scores meaningfully higher risk than an active/loyal cohort.
"""
from datetime import datetime, timezone

from app.ai.churn_model import DEFAULT_CALIBRATION, churn_risk_from_rfm, explain_churn_risk, score_all_members
from app.db.models import Member, Merchant


def test_freshly_active_high_frequency_member_scores_low_risk():
    score = churn_risk_from_rfm(recency_days=0, frequency=20, monetary=1000)
    assert score < 20


def test_long_dormant_zero_activity_member_scores_max_risk():
    score = churn_risk_from_rfm(recency_days=365, frequency=0, monetary=0)
    assert score == 100.0


def test_score_is_monotonic_in_recency():
    low = churn_risk_from_rfm(recency_days=1, frequency=5, monetary=100)
    high = churn_risk_from_rfm(recency_days=200, frequency=5, monetary=100)
    assert high > low


def test_score_is_monotonic_in_frequency():
    low_freq = churn_risk_from_rfm(recency_days=10, frequency=1, monetary=100)
    high_freq = churn_risk_from_rfm(recency_days=10, frequency=15, monetary=100)
    assert low_freq > high_freq


def test_lapsing_cohort_scores_meaningfully_higher_than_loyal_cohort(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    assert merchant is not None

    results = score_all_members(seeded_db, merchant.id)
    by_member = {r.member_id: r for r in results}

    members = seeded_db.query(Member).filter(Member.merchant_id == merchant.id).all()
    loyal_scores = [
        by_member[m.id].churn_risk_score for m in members if m.synthetic_cohort == "loyal"
    ]
    lapsing_scores = [
        by_member[m.id].churn_risk_score for m in members if m.synthetic_cohort == "lapsing"
    ]

    assert len(loyal_scores) > 20
    assert len(lapsing_scores) > 20

    avg_loyal = sum(loyal_scores) / len(loyal_scores)
    avg_lapsing = sum(lapsing_scores) / len(lapsing_scores)

    # "Meaningfully higher" -- require at least a 25-point gap on the 0-100
    # scale, not just any-positive-difference.
    assert avg_lapsing - avg_loyal >= 25.0, (
        f"expected lapsing cohort avg risk to exceed loyal cohort by >=25 points, "
        f"got loyal={avg_loyal:.1f} lapsing={avg_lapsing:.1f}"
    )

    # And the lapsing cohort should mostly land in medium/high risk bands.
    lapsing_bands = [by_member[m.id].risk_band for m in members if m.synthetic_cohort == "lapsing"]
    high_or_medium = sum(1 for b in lapsing_bands if b in ("medium", "high"))
    assert high_or_medium / len(lapsing_bands) >= 0.8

    loyal_bands = [by_member[m.id].risk_band for m in members if m.synthetic_cohort == "loyal"]
    low_or_medium = sum(1 for b in loyal_bands if b in ("low", "medium"))
    assert low_or_medium / len(loyal_bands) >= 0.8


def test_all_members_get_a_score(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    members = seeded_db.query(Member).filter(Member.merchant_id == merchant.id).all()
    results = score_all_members(seeded_db, merchant.id)
    assert len(results) == len(members)
    for r in results:
        assert 0.0 <= r.churn_risk_score <= 100.0
        assert r.risk_band in {"low", "medium", "high"}


# ---------------------------------------------------------------------------
# Plain-English "why" explanations (competitive-brief backlog item #5)
# ---------------------------------------------------------------------------


def test_explanation_names_recency_when_it_dominates():
    """Long-inactive but otherwise-normal-looking history -> recency should
    be named as the driver."""
    text = explain_churn_risk(
        recency_days=200.0, frequency=8, monetary=400.0, risk_band="high", calibration=DEFAULT_CALIBRATION
    )
    assert "since their last visit" in text
    assert "200 days" in text
    assert text.startswith("High risk")


def test_explanation_names_frequency_when_it_dominates():
    text = explain_churn_risk(
        recency_days=5.0, frequency=0, monetary=400.0, risk_band="medium", calibration=DEFAULT_CALIBRATION
    )
    assert "purchase" in text
    assert text.startswith("Medium risk")


def test_explanation_names_monetary_when_it_dominates():
    text = explain_churn_risk(
        recency_days=5.0, frequency=8, monetary=0.0, risk_band="medium", calibration=DEFAULT_CALIBRATION
    )
    assert "spent only" in text
    assert text.startswith("Medium risk")


def test_explanation_for_low_risk_is_reassuring_not_alarming():
    text = explain_churn_risk(
        recency_days=2.0, frequency=8, monetary=400.0, risk_band="low", calibration=DEFAULT_CALIBRATION
    )
    assert text.startswith("Low risk")
    assert "since their last visit" not in text  # doesn't borrow the high/medium phrasing


def test_explanation_handles_singular_purchase_grammar():
    text = explain_churn_risk(
        recency_days=5.0, frequency=1, monetary=400.0, risk_band="medium", calibration=DEFAULT_CALIBRATION
    )
    assert "1 purchase " in text
    assert "1 purchases" not in text


def test_score_all_members_populates_explanation(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    results = score_all_members(seeded_db, merchant.id)
    assert all(r.explanation for r in results)
    # Every explanation is a real sentence, not an empty default.
    assert all(len(r.explanation) > 15 for r in results)
