"""Fraud / anomaly flagging on point transactions (PLAN.md §3.3).

Two simple, explainable statistical rules -- deliberately not a black-box
model, so alerts are actionable for a merchant admin:

1. **Abnormal amount** (z-score): a transaction's $ amount is a strong
   outlier relative to that member's own transaction history (falls back to
   the whole-population distribution for members with too little history).
2. **Abnormal velocity**: a member racks up an unusually large number of
   earn transactions within a short rolling time window (points-farming /
   bot-like behavior).

Both are computed with pandas/numpy over the transaction table -- no
external services, deterministic given the same data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import FraudAlert, Member, Transaction, TransactionType

MIN_TXNS_FOR_MEMBER_STATS = 5


@dataclass
class FraudFinding:
    transaction_id: str
    member_id: str
    reason: str
    score: float
    details: str = ""


def _transactions_to_frame(transactions: list[Transaction]) -> pd.DataFrame:
    rows = []
    for t in transactions:
        created_at = t.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        rows.append(
            {
                "id": t.id,
                "member_id": t.member_id,
                "amount_usd": t.amount_usd,
                "created_at": created_at,
                "type": t.type,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("created_at").reset_index(drop=True)
    return df


def _amount_zscore_findings(df: pd.DataFrame, threshold: float) -> dict[str, FraudFinding]:
    findings: dict[str, FraudFinding] = {}
    if df.empty:
        return findings

    global_mean = df["amount_usd"].mean()
    global_std = df["amount_usd"].std(ddof=0) or 1.0

    for member_id, group in df.groupby("member_id"):
        if len(group) >= MIN_TXNS_FOR_MEMBER_STATS:
            mean = group["amount_usd"].mean()
            std = group["amount_usd"].std(ddof=0)
            if not std or std < 1e-6:
                std = global_std
        else:
            mean, std = global_mean, global_std

        z = (group["amount_usd"] - mean) / std
        outliers = group[z.abs() >= threshold]
        for idx, row in outliers.iterrows():
            zscore = float(z.loc[idx])
            findings[row["id"]] = FraudFinding(
                transaction_id=row["id"],
                member_id=member_id,
                reason="abnormal_amount",
                score=round(abs(zscore), 2),
                details=(
                    f"amount=${row['amount_usd']:.2f} is {abs(zscore):.1f} std-devs from "
                    f"member's typical ${mean:.2f} (n={len(group)})"
                ),
            )
    return findings


def _velocity_findings(
    df: pd.DataFrame, window_hours: int, max_txns: int
) -> dict[str, FraudFinding]:
    findings: dict[str, FraudFinding] = {}
    if df.empty:
        return findings

    window = pd.Timedelta(hours=window_hours)
    earn = df[df["type"] == TransactionType.EARN.value]

    for member_id, group in earn.groupby("member_id"):
        group = group.sort_values("created_at")
        times = group["created_at"].to_list()
        ids = group["id"].to_list()
        n = len(times)
        left = 0
        for right in range(n):
            while times[right] - times[left] > window:
                left += 1
            count_in_window = right - left + 1
            if count_in_window >= max_txns:
                score = round(count_in_window / max_txns, 2)
                for k in range(left, right + 1):
                    tid = ids[k]
                    existing = findings.get(tid)
                    if existing is None or score > existing.score:
                        findings[tid] = FraudFinding(
                            transaction_id=tid,
                            member_id=member_id,
                            reason="abnormal_velocity",
                            score=score,
                            details=(
                                f"{count_in_window} earn transactions within "
                                f"{window_hours}h window (threshold={max_txns})"
                            ),
                        )
    return findings


def detect_fraud(
    transactions: list[Transaction],
    zscore_threshold: float | None = None,
    velocity_window_hours: int | None = None,
    velocity_max_txns: int | None = None,
) -> list[FraudFinding]:
    """Pure function over a list of Transaction ORM objects -> findings.
    No DB writes -- easy to unit test deterministically."""
    zscore_threshold = zscore_threshold if zscore_threshold is not None else settings.fraud_zscore_threshold
    velocity_window_hours = (
        velocity_window_hours if velocity_window_hours is not None else settings.fraud_velocity_window_hours
    )
    velocity_max_txns = (
        velocity_max_txns if velocity_max_txns is not None else settings.fraud_velocity_max_txns
    )

    df = _transactions_to_frame(transactions)
    amount_findings = _amount_zscore_findings(df, zscore_threshold)
    velocity_findings = _velocity_findings(df, velocity_window_hours, velocity_max_txns)

    combined: dict[str, FraudFinding] = dict(amount_findings)
    for tid, finding in velocity_findings.items():
        if tid in combined:
            # merge: keep both reasons, take max score
            existing = combined[tid]
            combined[tid] = FraudFinding(
                transaction_id=tid,
                member_id=finding.member_id,
                reason=f"{existing.reason}+{finding.reason}",
                score=max(existing.score, finding.score),
                details=f"{existing.details}; {finding.details}",
            )
        else:
            combined[tid] = finding

    return list(combined.values())


def run_fraud_detection(db: Session, merchant_id: str) -> list[FraudAlert]:
    """Run detection over all of a merchant's transactions and persist any
    newly-found alerts (idempotent: won't double-alert a transaction that
    already has an alert row)."""
    transactions = (
        db.query(Transaction)
        .join(Member, Transaction.member_id == Member.id)
        .filter(Member.merchant_id == merchant_id)
        .all()
    )
    findings = detect_fraud(transactions)

    existing_alert_txn_ids = {
        row[0]
        for row in db.query(FraudAlert.transaction_id)
        .join(Member, FraudAlert.member_id == Member.id)
        .filter(Member.merchant_id == merchant_id)
        .all()
    }

    created: list[FraudAlert] = []
    for finding in findings:
        if finding.transaction_id in existing_alert_txn_ids:
            continue
        alert = FraudAlert(
            transaction_id=finding.transaction_id,
            member_id=finding.member_id,
            reason=finding.reason,
            score=finding.score,
            details=finding.details,
        )
        db.add(alert)
        created.append(alert)

    db.flush()
    return created
