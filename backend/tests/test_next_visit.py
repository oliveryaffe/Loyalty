"""Predicted next visit/order date (app/ai/next_visit.py) -- competitor
research finding (Klaviyo surfaces a "predicted order date" per contact
alongside churn risk/CLV)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.ai.next_visit import predict_next_visit, predict_next_visit_for_all_members
from app.db.models import Member, Merchant, Transaction, TransactionType

NOW = datetime.now(timezone.utc)


def _merchant(db_session, name="Next Visit Test Co") -> Merchant:
    m = Merchant(business_name=name)
    db_session.add(m)
    db_session.flush()
    return m


def _member(db_session, merchant_id, label) -> Member:
    member = Member(
        merchant_id=merchant_id,
        first_name=label,
        last_name="Test",
        email=f"{label.lower()}@example.com",
        points_balance=0,
        last_activity_at=NOW,
    )
    db_session.add(member)
    db_session.flush()
    return member


def _earn(db_session, member_id, days_ago):
    db_session.add(
        Transaction(
            member_id=member_id,
            type=TransactionType.EARN.value,
            amount_gbp=10.0,
            points=10,
            created_at=NOW - timedelta(days=days_ago),
        )
    )


def test_uses_members_own_median_gap_once_enough_purchases(db_session):
    m = _merchant(db_session)
    member = _member(db_session, m.id, "Regular")
    # Visits every ~7 days, last one 3 days ago -- own rhythm should
    # predict roughly 4 days from now.
    for days_ago in [3, 10, 17, 24]:
        _earn(db_session, member.id, days_ago)
    db_session.commit()
    db_session.refresh(member)

    result = predict_next_visit(member, merchant_typical_interval_days=None, now=NOW)

    assert result.source == "member"
    assert result.typical_interval_days == pytest.approx(7.0, abs=0.1)
    assert result.predicted_next_visit_date == (NOW - timedelta(days=3) + timedelta(days=7)).date()
    assert result.days_overdue is None  # predicted date is still in the future


def test_falls_back_to_merchant_wide_interval_for_new_member(db_session):
    m = _merchant(db_session)
    member = _member(db_session, m.id, "Newish")
    # Only 2 purchases -- below MIN_OWN_PURCHASES_FOR_MEMBER_RHYTHM (3).
    _earn(db_session, member.id, 20)
    _earn(db_session, member.id, 5)
    db_session.commit()
    db_session.refresh(member)

    result = predict_next_visit(member, merchant_typical_interval_days=14.0, now=NOW)

    assert result.source == "merchant"
    assert result.typical_interval_days == 14.0
    assert result.predicted_next_visit_date == (NOW - timedelta(days=5) + timedelta(days=14)).date()


def test_insufficient_data_with_no_purchases_and_no_fallback(db_session):
    m = _merchant(db_session)
    member = _member(db_session, m.id, "BrandNew")
    db_session.commit()
    db_session.refresh(member)

    result = predict_next_visit(member, merchant_typical_interval_days=None, now=NOW)

    assert result.source == "insufficient_data"
    assert result.predicted_next_visit_date is None
    assert result.days_overdue is None


def test_marks_overdue_when_predicted_date_has_passed(db_session):
    m = _merchant(db_session)
    member = _member(db_session, m.id, "Overdue")
    # Regular 7-day rhythm, but last visit was 30 days ago -- well past
    # due for a return visit.
    for days_ago in [30, 37, 44, 51]:
        _earn(db_session, member.id, days_ago)
    db_session.commit()
    db_session.refresh(member)

    result = predict_next_visit(member, merchant_typical_interval_days=None, now=NOW)

    assert result.source == "member"
    assert result.days_overdue is not None
    assert result.days_overdue >= 22  # ~30 - 7 = 23 days overdue


def test_predict_next_visit_for_all_members_computes_fallback_once(db_session):
    m = _merchant(db_session)
    # A handful of members with an established ~10-day rhythm each, to
    # give the merchant-wide fallback enough gaps to be reliable.
    for i in range(3):
        member = _member(db_session, m.id, f"Established{i}")
        for days_ago in [2, 12, 22, 32]:
            _earn(db_session, member.id, days_ago)
    # One brand-new member with a single purchase -- should use the
    # merchant-wide fallback above rather than staying "insufficient_data".
    new_member = _member(db_session, m.id, "JustJoined")
    _earn(db_session, new_member.id, 1)
    db_session.commit()

    results = predict_next_visit_for_all_members(db_session, m.id)
    by_member = {r.member_id: r for r in results}

    assert by_member[new_member.id].source == "merchant"
    assert by_member[new_member.id].predicted_next_visit_date is not None
