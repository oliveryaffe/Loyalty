"""Cross-merchant vertical benchmarking (competitive-brief backlog item
#3): "your repeat-visit rate is in the top 20% of UK coffee shops" is a
claim only Ledgerly can make once it has enough tenants in a vertical --
none of the named competitors (single-merchant loyalty apps, single-tenant
enterprise CLV tools) have the multi-tenant, multi-vertical data position
to do this credibly. This is the one feature in the backlog that only gets
more valuable as the merchant base grows and is worth essentially nothing
on day one with a handful of accounts -- see MIN_PEER_MERCHANTS below and
its "insufficient data" fallback, which is the expected, honest state for
a young multi-tenant base rather than a bug.

Metric: repeat-visit rate (share of members with 2+ EARN transactions
ever) -- chosen because it's the exact example used in the competitive
brief, is comparable across verticals without unit conversion (unlike
average order value, which a coffee shop and a retailer will never share
a scale for), and only needs a merchant's own member/transaction data to
compute, not anything vertical-specific.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Member, Merchant, Transaction, TransactionType
from app.services.sample_data import has_real_data

# Below this many *other* real-data merchants in the same vertical, a
# percentile is more noise than signal (one or two peers can swing a
# ranking wildly) -- show "not enough peer data yet" instead of a
# misleadingly precise-looking number.
MIN_PEER_MERCHANTS = 3


def _repeat_visit_rate(db: Session, merchant_id: str) -> float | None:
    """Share of this merchant's members with 2+ lifetime EARN transactions.
    None (not 0.0) if the merchant has zero members -- there is no
    meaningful rate to compute, and 0.0 would misleadingly look like "we
    checked and it's zero" rather than "there's nothing here yet"."""
    total_members = db.query(func.count(Member.id)).filter(Member.merchant_id == merchant_id).scalar()
    if not total_members:
        return None

    repeat_members = (
        db.query(Transaction.member_id)
        .join(Member, Transaction.member_id == Member.id)
        .filter(Member.merchant_id == merchant_id, Transaction.type == TransactionType.EARN.value)
        .group_by(Transaction.member_id)
        .having(func.count(Transaction.id) >= 2)
        .count()
    )
    return repeat_members / total_members


@dataclass(frozen=True)
class BenchmarkResult:
    available: bool
    business_type: str | None
    peer_count: int
    your_repeat_visit_rate: float | None
    top_percent: int | None
    message: str


def compute_benchmark(db: Session, merchant: Merchant) -> BenchmarkResult:
    if not merchant.business_type:
        return BenchmarkResult(
            available=False,
            business_type=None,
            peer_count=0,
            your_repeat_visit_rate=None,
            top_percent=None,
            message="Set your business type in Settings to unlock benchmarking against similar businesses.",
        )

    if not has_real_data(db, merchant.id):
        return BenchmarkResult(
            available=False,
            business_type=merchant.business_type,
            peer_count=0,
            your_repeat_visit_rate=None,
            top_percent=None,
            message="Upload your own transaction data (sample data doesn't count) to see how you compare.",
        )

    your_rate = _repeat_visit_rate(db, merchant.id)
    if your_rate is None:
        return BenchmarkResult(
            available=False,
            business_type=merchant.business_type,
            peer_count=0,
            your_repeat_visit_rate=None,
            top_percent=None,
            message="Not enough of your own data yet to compute a repeat-visit rate.",
        )

    peer_merchants = (
        db.query(Merchant)
        .filter(Merchant.business_type == merchant.business_type, Merchant.id != merchant.id)
        .all()
    )
    peer_rates: list[float] = []
    for peer in peer_merchants:
        if not has_real_data(db, peer.id):
            continue
        rate = _repeat_visit_rate(db, peer.id)
        if rate is not None:
            peer_rates.append(rate)

    if len(peer_rates) < MIN_PEER_MERCHANTS:
        return BenchmarkResult(
            available=False,
            business_type=merchant.business_type,
            peer_count=len(peer_rates),
            your_repeat_visit_rate=your_rate,
            top_percent=None,
            message=(
                f"Not enough other {merchant.business_type.replace('_', ' ')} businesses on Ledgerly yet "
                f"to benchmark against ({len(peer_rates)} so far, need {MIN_PEER_MERCHANTS}) -- check back "
                f"as more join."
            ),
        )

    all_rates_desc = sorted(peer_rates + [your_rate], reverse=True)
    rank = all_rates_desc.index(your_rate) + 1  # 1 = best in the pool
    total = len(all_rates_desc)
    top_percent = math.ceil(100 * rank / total)

    label = merchant.business_type.replace("_", " ")
    message = (
        f"Your repeat-visit rate ({your_rate:.0%}) is in the top {top_percent}% of {label} businesses "
        f"on Ledgerly ({total} compared)."
    )

    return BenchmarkResult(
        available=True,
        business_type=merchant.business_type,
        peer_count=len(peer_rates),
        your_repeat_visit_rate=your_rate,
        top_percent=top_percent,
        message=message,
    )
