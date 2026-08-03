"""CSV backfill ingestion service tests (app/services/csv_ingest.py).

Covers the acceptance criteria from PLAN_BATCH2.md §2/§"Acceptance
criteria": valid rows ingest, malformed rows are skipped and reported with
correct 1-based row numbers, missing required headers rejects the whole
file, mint_points defaults to false (existing points_balance is NOT
affected), mint_points=true does credit points, and external_order_id
re-upload idempotency.
"""
import pytest

from app.db.models import Member, Merchant, Transaction, TransactionType
from app.services.csv_ingest import CsvUploadError, parse_and_ingest_csv

HEADER = "customer_email,customer_first_name,customer_last_name,transaction_date,amount_gbp,product_category,product_name,channel,external_order_id"


@pytest.fixture()
def merchant(db_session):
    m = Merchant(business_name="CSV Test Co")
    db_session.add(m)
    db_session.flush()
    return m


@pytest.fixture()
def existing_member(db_session, merchant):
    member = Member(
        merchant_id=merchant.id,
        first_name="Existing",
        last_name="Member",
        email="existing@example.com",
        points_balance=250,
    )
    db_session.add(member)
    db_session.flush()
    return member


def _csv(rows: list[str]) -> bytes:
    return ("\n".join([HEADER] + rows)).encode("utf-8")


def test_valid_rows_ingest_and_create_new_member(db_session, merchant):
    body = _csv(
        [
            "new@example.com,Ada,Lovelace,2026-05-01,42.50,beverage,Cold Brew,pos,ORD-1",
            "new@example.com,Ada,Lovelace,2026-05-02,10.00,bakery,Muffin,pos,ORD-2",
        ]
    )
    result = parse_and_ingest_csv(db_session, merchant, body, mint_points=False)

    assert result.rows_received == 2
    assert result.rows_ingested == 2
    assert result.rows_failed == 0
    assert result.rows_skipped_duplicate == 0
    assert result.members_created == 1
    assert result.errors == []

    member = db_session.query(Member).filter(Member.email == "new@example.com").first()
    assert member is not None
    assert member.first_name == "Ada"

    txns = db_session.query(Transaction).filter(Transaction.member_id == member.id).all()
    assert len(txns) == 2
    assert all(t.source == "csv_upload" for t in txns)
    assert all(t.type == TransactionType.EARN.value for t in txns)
    categories = {t.product_category for t in txns}
    assert categories == {"beverage", "bakery"}


def test_malformed_rows_are_skipped_and_reported_with_correct_row_numbers(db_session, merchant):
    rows = [f"good{i}@example.com,A,B,2026-05-0{i%9+1},10.00,beverage,X,pos,ORD-GOOD-{i}" for i in range(1, 11)]
    rows.append("bad-date@example.com,A,B,not-a-date,10.00,beverage,X,pos,ORD-BAD-1")
    rows.append("bad-amount@example.com,A,B,2026-05-01,not-a-number,beverage,X,pos,ORD-BAD-2")
    body = _csv(rows)

    result = parse_and_ingest_csv(db_session, merchant, body, mint_points=False)

    assert result.rows_received == 12
    assert result.rows_ingested == 10
    assert result.rows_failed == 2
    assert len(result.errors) == 2
    # Row 12 is the unparseable date (10 good rows + header = row 12 is the
    # 11th data row -> 1-based line 12), row 13 is the bad amount.
    error_rows = {e.row for e in result.errors}
    assert error_rows == {12, 13}


def test_missing_required_header_rejects_whole_file(db_session, merchant):
    body = b"customer_email,transaction_date\nfoo@example.com,2026-05-01"  # missing amount_gbp
    with pytest.raises(CsvUploadError):
        parse_and_ingest_csv(db_session, merchant, body, mint_points=False)
    assert db_session.query(Transaction).count() == 0


def test_empty_file_rejected(db_session, merchant):
    with pytest.raises(CsvUploadError):
        parse_and_ingest_csv(db_session, merchant, b"", mint_points=False)


def test_amount_out_of_range_is_row_level_failure(db_session, merchant):
    body = _csv(
        [
            "x@example.com,A,B,2026-05-01,0,beverage,X,pos,ORD-1",
            "y@example.com,A,B,2026-05-01,-5,beverage,X,pos,ORD-2",
            "z@example.com,A,B,2026-05-01,999999999,beverage,X,pos,ORD-3",
        ]
    )
    result = parse_and_ingest_csv(db_session, merchant, body, mint_points=False)
    assert result.rows_ingested == 0
    assert result.rows_failed == 3


def test_invalid_email_is_row_level_failure(db_session, merchant):
    body = _csv(["not-an-email,A,B,2026-05-01,10.00,beverage,X,pos,ORD-1"])
    result = parse_and_ingest_csv(db_session, merchant, body, mint_points=False)
    assert result.rows_ingested == 0
    assert result.rows_failed == 1
    assert "customer_email" in result.errors[0].reason


def test_mint_points_defaults_false_and_does_not_change_existing_balance(db_session, merchant, existing_member):
    balance_before = existing_member.points_balance
    body = _csv(["existing@example.com,Existing,Member,2026-05-01,50.00,beverage,Cold Brew,pos,ORD-BACKFILL-1"])

    result = parse_and_ingest_csv(db_session, merchant, body, mint_points=False)

    assert result.rows_ingested == 1
    db_session.refresh(existing_member)
    assert existing_member.points_balance == balance_before

    txn = db_session.query(Transaction).filter(Transaction.external_order_id == "ORD-BACKFILL-1").first()
    assert txn is not None
    assert txn.points == 0


def test_mint_points_true_credits_real_balance(db_session, merchant, existing_member):
    balance_before = existing_member.points_balance
    body = _csv(["existing@example.com,Existing,Member,2026-05-01,50.00,beverage,Cold Brew,pos,ORD-MINT-1"])

    result = parse_and_ingest_csv(db_session, merchant, body, mint_points=True)

    assert result.rows_ingested == 1
    db_session.refresh(existing_member)
    assert existing_member.points_balance == balance_before + 50  # 1:1 points_per_pound, floored


def test_reuploading_same_file_is_idempotent_via_external_order_id(db_session, merchant):
    body = _csv(["dup@example.com,A,B,2026-05-01,10.00,beverage,X,pos,ORD-DUP-1"])

    first = parse_and_ingest_csv(db_session, merchant, body, mint_points=False)
    db_session.commit()
    assert first.rows_ingested == 1
    assert first.rows_skipped_duplicate == 0

    second = parse_and_ingest_csv(db_session, merchant, body, mint_points=False)
    db_session.commit()
    assert second.rows_ingested == 0
    assert second.rows_skipped_duplicate == 1

    assert db_session.query(Transaction).filter(Transaction.external_order_id == "ORD-DUP-1").count() == 1


def test_duplicate_external_order_id_within_same_file_is_skipped(db_session, merchant):
    body = _csv(
        [
            "a@example.com,A,B,2026-05-01,10.00,beverage,X,pos,ORD-SAME",
            "b@example.com,A,B,2026-05-02,20.00,beverage,X,pos,ORD-SAME",
        ]
    )
    result = parse_and_ingest_csv(db_session, merchant, body, mint_points=False)
    assert result.rows_ingested == 1
    assert result.rows_skipped_duplicate == 1


def test_row_without_external_order_id_is_never_treated_as_duplicate(db_session, merchant):
    body = _csv(
        [
            "a@example.com,A,B,2026-05-01,10.00,beverage,X,pos,",
            "a@example.com,A,B,2026-05-02,10.00,beverage,X,pos,",
        ]
    )
    result = parse_and_ingest_csv(db_session, merchant, body, mint_points=False)
    assert result.rows_ingested == 2
    assert result.rows_skipped_duplicate == 0


def test_channel_defaults_to_pos_when_blank(db_session, merchant):
    body = _csv(["a@example.com,A,B,2026-05-01,10.00,beverage,X,,ORD-1"])
    result = parse_and_ingest_csv(db_session, merchant, body, mint_points=False)
    assert result.rows_ingested == 1
    txn = db_session.query(Transaction).filter(Transaction.external_order_id == "ORD-1").first()
    assert txn.channel == "pos"


def test_row_level_failures_never_abort_the_rest_of_the_file(db_session, merchant):
    body = _csv(
        [
            "good1@example.com,A,B,2026-05-01,10.00,beverage,X,pos,ORD-1",
            "bad@example.com,A,B,garbage-date,10.00,beverage,X,pos,ORD-2",
            "good2@example.com,A,B,2026-05-03,10.00,beverage,X,pos,ORD-3",
        ]
    )
    result = parse_and_ingest_csv(db_session, merchant, body, mint_points=False)
    assert result.rows_ingested == 2
    assert result.rows_failed == 1
