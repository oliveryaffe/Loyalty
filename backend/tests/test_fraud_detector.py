"""Fraud/anomaly detection tests (app/ai/fraud_detector.py).

Covers the pure `detect_fraud` function on a small hand-built dataset, plus
-- against the real seeded synthetic dataset -- the acceptance criterion
that intentionally-injected fraud-like transactions actually get flagged,
while the false-positive rate on normal transactions stays low (<5%).
"""
from datetime import datetime, timedelta, timezone

from app.ai.fraud_detector import detect_fraud, run_fraud_detection
from app.db.models import FraudAlert, Merchant, Transaction, TransactionType


def _mk_txn(id_, member_id, amount, created_at, type_=TransactionType.EARN.value):
    return Transaction(
        id=id_,
        member_id=member_id,
        type=type_,
        amount_usd=amount,
        points=int(amount),
        created_at=created_at,
    )


def test_amount_outlier_is_flagged():
    base = datetime.now(timezone.utc)
    txns = []
    # Member "m1" has 10 normal small purchases...
    for i in range(10):
        txns.append(_mk_txn(f"n{i}", "m1", 20.0 + i, base - timedelta(days=30 - i)))
    # ...then one wildly oversized purchase.
    spike = _mk_txn("spike", "m1", 3000.0, base - timedelta(days=1))
    txns.append(spike)

    findings = detect_fraud(txns, zscore_threshold=3.0, velocity_window_hours=24, velocity_max_txns=100)
    flagged_ids = {f.transaction_id for f in findings}
    assert "spike" in flagged_ids


def test_normal_uniform_transactions_are_not_flagged():
    base = datetime.now(timezone.utc)
    txns = [
        _mk_txn(f"n{i}", "m2", 25.0 + (i % 3), base - timedelta(days=60 - i)) for i in range(20)
    ]
    findings = detect_fraud(txns, zscore_threshold=3.0, velocity_window_hours=24, velocity_max_txns=100)
    assert findings == []


def test_velocity_burst_is_flagged():
    base = datetime.now(timezone.utc)
    txns = []
    # Normal spaced-out history.
    for i in range(5):
        txns.append(_mk_txn(f"n{i}", "m3", 20.0, base - timedelta(days=30 - i * 3)))
    # A burst of 10 transactions within 1 hour.
    for i in range(10):
        txns.append(_mk_txn(f"b{i}", "m3", 15.0, base - timedelta(minutes=60 - i * 5)))

    findings = detect_fraud(txns, zscore_threshold=100.0, velocity_window_hours=24, velocity_max_txns=5)
    flagged_ids = {f.transaction_id for f in findings}
    assert any(fid.startswith("b") for fid in flagged_ids)


def test_run_fraud_detection_against_seeded_data_recall_and_false_positive_rate(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    assert merchant is not None

    run_fraud_detection(seeded_db, merchant.id)
    seeded_db.commit()

    all_txns = (
        seeded_db.query(Transaction)
        .join(Transaction.member)
        .filter(Transaction.member.has(merchant_id=merchant.id))
        .all()
    )
    fraud_labeled = [t for t in all_txns if t.synthetic_fraud_label]
    normal = [t for t in all_txns if not t.synthetic_fraud_label]
    assert len(fraud_labeled) > 20
    assert len(normal) > 1000

    alerted_txn_ids = {
        row[0]
        for row in seeded_db.query(FraudAlert.transaction_id)
        .join(FraudAlert.member)
        .filter(FraudAlert.member.has(merchant_id=merchant.id))
        .all()
    }

    recall = sum(1 for t in fraud_labeled if t.id in alerted_txn_ids) / len(fraud_labeled)
    false_positive_rate = sum(1 for t in normal if t.id in alerted_txn_ids) / len(normal)

    assert recall >= 0.7, f"expected recall >= 0.7 on injected fraud, got {recall:.2%}"
    assert false_positive_rate < 0.05, (
        f"expected false-positive rate < 5% on normal transactions, got {false_positive_rate:.2%}"
    )


def test_fraud_detection_is_idempotent(seeded_db):
    merchant = seeded_db.query(Merchant).first()
    created_first = run_fraud_detection(seeded_db, merchant.id)
    seeded_db.commit()
    created_second = run_fraud_detection(seeded_db, merchant.id)
    seeded_db.commit()

    # Second run shouldn't re-alert transactions already alerted.
    assert len(created_second) == 0
