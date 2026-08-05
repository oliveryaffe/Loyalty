"""Churn / attrition risk scoring (PLAN.md §3.2).

RFM (Recency / Frequency / Monetary) based scoring. Each member gets a
0-100 risk score where higher = more likely to disengage. Deliberately not
a trained classifier at MVP scale (no labeled churn outcome exists yet) --
architected as a scoring function with clearly named, swappable thresholds
so a real supervised model can replace `churn_risk_from_rfm` later without
touching the API layer.

Per-merchant calibration (added after shipping): the thresholds below were
originally fixed module constants, implicitly tuned around one kind of
business -- a frequent, low-basket-size habit purchase, i.e. a coffee shop.
A customer visiting a barber every 5 weeks, or a clothing shop twice a
year, is just as "loyal" as a coffee-shop regular visiting twice a week --
but on a completely different clock. Scoring every merchant against the
same fixed 90-day recency cutoff / 8-visits-per-120-days / £400 monetary
bar would silently mis-score anyone whose business doesn't look like a
coffee shop.

`compute_merchant_calibration` below derives those thresholds from each
merchant's OWN transaction history instead. First attempt (worth recording
so it isn't repeated): deriving the lookback window from the median gap
between a member's consecutive purchases, then deriving frequency/monetary
saturation *algebraically* from that same gap (window / gap = "expected
visit count"), badly over-flagged normal customers -- real visit patterns
cluster in bursts rather than spacing out evenly, so the median gap is
dominated by the short gaps *within* a burst, and a window sized off it
systematically undercounts how many visits a normal customer actually
racks up over a longer period. Verified against the seeded coffee-shop
data: that approach flagged 84% of the deliberately-designed-to-be-healthy
"average" cohort as high risk.

The fix: still use the median per-member gap to size a sensible
observation window (long enough to be informative), but then derive the
recency/frequency/monetary saturation points *empirically* -- actually
measure what recency/frequency/monetary look like across this merchant's
whole member base within that window, and set "zero risk" / "max risk" at
a percentile of the real observed distribution, not an assumed formula.
This is what actually adapts correctly to a business's real rhythm
regardless of how bursty or regular its customers' visit patterns are.
Merchants with too little repeat-purchase history to calibrate reliably
fall back to exactly the original fixed constants (now named
`DEFAULT_CALIBRATION`) -- same "honest fallback" pattern used everywhere
else in this AI layer (see future_value.py's model_used="heuristic" path).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.db.models import Member, Merchant, Transaction, TransactionType

# Original tuning constants -- still the literal values used whenever a
# merchant can't be calibrated (see DEFAULT_CALIBRATION below). Kept as
# named module constants (rather than inlined into DEFAULT_CALIBRATION)
# since other modules already import LOOKBACK_DAYS/HOLDOUT_DAYS by name.
RECENCY_SATURATION_DAYS = 90.0  # inactivity at/beyond this = max recency risk
FREQUENCY_SATURATION = 8.0  # earn-transactions in lookback window for zero freq risk
MONETARY_SATURATION = 400.0  # £ spent in lookback window for zero monetary risk
LOOKBACK_DAYS = 120  # window used for frequency/monetary components
HOLDOUT_DAYS = 45  # future_value.py's backtest split window -- lives here too now that both share MerchantCalibration

WEIGHT_RECENCY = 0.5
WEIGHT_FREQUENCY = 0.3
WEIGHT_MONETARY = 0.2

RISK_BAND_LOW_MAX = 35.0
RISK_BAND_MEDIUM_MAX = 65.0

# --- Calibration derivation tuning. These ARE global/fixed -- they encode
# "how much of the base should read as zero-risk vs. worrying", a scoring
# philosophy choice, not a business-type fact -- so they don't themselves
# need per-merchant calibration. Chosen by tuning against the seeded
# coffee-shop dataset (which has known-good cohort labels) until the
# resulting band distribution matched the original hand-tuned
# DEFAULT_CALIBRATION's behavior on that same data -- see module docstring.
MIN_MEMBERS_WITH_REPEAT_VISITS = 20  # below this, a median interval isn't statistically reliable -- use DEFAULT_CALIBRATION
LOOKBACK_CYCLES = 12.0  # observation window spans ~12 typical visit cycles -- wide enough that bursty visit patterns still show up
HOLDOUT_CYCLES = 4.0  # future-value backtest holdout spans ~4 typical visit cycles
RECENCY_PERCENTILE = 70  # members inactive longer than this percentile of the base = maximum recency risk
FREQ_MONETARY_PERCENTILE = 40  # members below this percentile of observed frequency/monetary get some risk -- i.e. the top ~60% of the base counts as "zero risk" on that dimension
MIN_LOOKBACK_DAYS, MAX_LOOKBACK_DAYS = 30.0, 365.0
MIN_RECENCY_DAYS, MAX_RECENCY_DAYS = 14.0, 270.0
MIN_HOLDOUT_DAYS, MAX_HOLDOUT_DAYS = 30.0, 180.0


@dataclass(frozen=True)
class MerchantCalibration:
    """Everything downstream (churn scoring, future-value features and
    backtest window) that used to read fixed module constants now reads
    one of these instead. `source` is surfaced in the honest-framing spirit
    of this codebase (mirrors future_value's `model_used`) so it's always
    visible whether a given score used real per-merchant calibration or
    the generic fallback."""

    lookback_days: float
    recency_saturation_days: float
    frequency_saturation: float
    monetary_saturation: float
    holdout_days: float
    source: Literal["calibrated", "default", "default_vertical"]


DEFAULT_CALIBRATION = MerchantCalibration(
    lookback_days=float(LOOKBACK_DAYS),
    recency_saturation_days=RECENCY_SATURATION_DAYS,
    frequency_saturation=FREQUENCY_SATURATION,
    monetary_saturation=MONETARY_SATURATION,
    holdout_days=float(HOLDOUT_DAYS),
    source="default",
)

# Onboarding business-type picker (see Merchant.business_type): a rough,
# vertical-informed starting point for a merchant with too little of their
# own transaction history yet for compute_merchant_calibration's real,
# empirical calibration to kick in (MIN_MEMBERS_WITH_REPEAT_VISITS below).
# These are day-one defaults, not precision-tuned against real data the
# way DEFAULT_CALIBRATION's coffee-shop numbers were verified against the
# seeded dataset -- they get replaced automatically the moment a merchant
# has enough repeat-visit history, same fallback mechanism as
# DEFAULT_CALIBRATION. `source="default_vertical"` (vs. plain "default")
# keeps that honestly distinguishable from the true generic fallback for
# anyone who picked "other" or hasn't onboarded yet.
#
# Each profile reasons from a typical repeat-visit interval for that
# vertical using the same LOOKBACK_CYCLES/HOLDOUT_CYCLES/day-bound
# constants real calibration uses, plus a rough UK average-ticket-size
# assumption for the monetary bar -- not measured, just a sane starting
# point.
BUSINESS_TYPE_CALIBRATIONS: dict[str, MerchantCalibration] = {
    # ~weekly habit purchase, low basket -- identical to DEFAULT_CALIBRATION,
    # since that was originally tuned around a coffee shop.
    "coffee_shop": MerchantCalibration(
        lookback_days=120.0,
        recency_saturation_days=90.0,
        frequency_saturation=8.0,
        monetary_saturation=400.0,
        holdout_days=45.0,
        source="default_vertical",
    ),
    # ~every 2-3 weeks, higher ticket than a coffee shop.
    "restaurant": MerchantCalibration(
        lookback_days=252.0,
        recency_saturation_days=150.0,
        frequency_saturation=6.0,
        monetary_saturation=250.0,
        holdout_days=84.0,
        source="default_vertical",
    ),
    # ~every 5-6 weeks (haircut regrowth cycle), mid ticket.
    "barber_salon": MerchantCalibration(
        lookback_days=MAX_LOOKBACK_DAYS,
        recency_saturation_days=180.0,
        frequency_saturation=5.0,
        monetary_saturation=150.0,
        holdout_days=140.0,
        source="default_vertical",
    ),
    # ~quarterly, higher ticket, low visit frequency by design.
    "retail": MerchantCalibration(
        lookback_days=MAX_LOOKBACK_DAYS,
        recency_saturation_days=MAX_RECENCY_DAYS,
        frequency_saturation=2.0,
        monetary_saturation=180.0,
        holdout_days=MAX_HOLDOUT_DAYS,
        source="default_vertical",
    ),
    # "other"/unset intentionally omitted -- falls through to
    # DEFAULT_CALIBRATION in compute_merchant_calibration below, same as a
    # merchant that never picked a business type at all.
}


@dataclass
class ChurnResult:
    member_id: str
    first_name: str
    last_name: str
    recency_days: float
    frequency: int
    monetary: float
    churn_risk_score: float
    risk_band: str


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _risk_band(score: float) -> str:
    if score < RISK_BAND_LOW_MAX:
        return "low"
    if score < RISK_BAND_MEDIUM_MAX:
        return "medium"
    return "high"


def compute_merchant_calibration(
    db: Session, merchant_id: str, now: datetime | None = None
) -> MerchantCalibration:
    """Derive this merchant's own RFM/future-value thresholds from its
    transaction history. See module docstring for the two-step method and
    why the saturation points are measured empirically rather than derived
    algebraically from the gap statistic alone.

    Falls back to BUSINESS_TYPE_CALIBRATIONS[merchant.business_type] (or
    DEFAULT_CALIBRATION if unset/unrecognised) when there isn't enough
    repeat-visit history yet -- see Merchant.business_type and the
    BUSINESS_TYPE_CALIBRATIONS docstring above."""
    now = now or datetime.now(timezone.utc)

    members = db.query(Member).filter(Member.merchant_id == merchant_id).all()
    txns = (
        db.query(Transaction)
        .join(Member, Transaction.member_id == Member.id)
        .filter(Member.merchant_id == merchant_id, Transaction.type == TransactionType.EARN.value)
        .order_by(Transaction.member_id, Transaction.created_at)
        .all()
    )
    by_member: dict[str, list[Transaction]] = {}
    for t in txns:
        by_member.setdefault(t.member_id, []).append(t)

    # Step 1: typical repeat-visit interval. Each member contributes their
    # OWN median gap, then we take the median across members -- weighting
    # every member equally regardless of how many transactions they have,
    # so a handful of very frequent visitors can't dominate the estimate
    # the way pooling every raw gap together would.
    per_member_median_gap: list[float] = []
    for member_txns in by_member.values():
        if len(member_txns) < 2:
            continue
        gaps = [
            (_aware(cur.created_at) - _aware(prev.created_at)).total_seconds() / 86400.0
            for prev, cur in zip(member_txns, member_txns[1:])
        ]
        gaps = [g for g in gaps if g > 0]
        if gaps:
            per_member_median_gap.append(statistics.median(gaps))

    if len(per_member_median_gap) < MIN_MEMBERS_WITH_REPEAT_VISITS:
        merchant = db.get(Merchant, merchant_id)
        business_type = merchant.business_type if merchant is not None else None
        return BUSINESS_TYPE_CALIBRATIONS.get(business_type, DEFAULT_CALIBRATION)

    median_interval = max(statistics.median(per_member_median_gap), 0.5)
    lookback_days = _clamp(median_interval * LOOKBACK_CYCLES, MIN_LOOKBACK_DAYS, MAX_LOOKBACK_DAYS)
    holdout_days = _clamp(median_interval * HOLDOUT_CYCLES, MIN_HOLDOUT_DAYS, MAX_HOLDOUT_DAYS)

    # Step 2: with the window size settled, measure what recency/frequency/
    # monetary actually look like across this merchant's own member base
    # within that window, and set the saturation points from the real
    # observed distribution.
    window_start_ts = now.timestamp() - lookback_days * 86400.0
    recencies: list[float] = []
    frequencies: list[int] = []
    monetaries: list[float] = []
    for m in members:
        last_activity = _aware(m.last_activity_at)
        recencies.append(max(0.0, (now - last_activity).total_seconds() / 86400.0))
        member_txns = by_member.get(m.id, [])
        in_window = [t for t in member_txns if _aware(t.created_at).timestamp() >= window_start_ts]
        frequencies.append(len(in_window))
        monetaries.append(sum(t.amount_gbp for t in in_window))

    recency_saturation_days = _clamp(
        statistics.quantiles(recencies, n=100)[RECENCY_PERCENTILE - 1], MIN_RECENCY_DAYS, MAX_RECENCY_DAYS
    )
    frequency_saturation = max(2.0, statistics.quantiles(frequencies, n=100)[FREQ_MONETARY_PERCENTILE - 1])
    monetary_saturation = max(1.0, statistics.quantiles(monetaries, n=100)[FREQ_MONETARY_PERCENTILE - 1])

    return MerchantCalibration(
        lookback_days=lookback_days,
        recency_saturation_days=recency_saturation_days,
        frequency_saturation=frequency_saturation,
        monetary_saturation=monetary_saturation,
        holdout_days=holdout_days,
        source="calibrated",
    )


def churn_risk_from_rfm(
    recency_days: float,
    frequency: int,
    monetary: float,
    calibration: MerchantCalibration = DEFAULT_CALIBRATION,
) -> float:
    """Pure function: RFM features -> 0-100 risk score. Higher = riskier.
    `calibration` defaults to the original fixed constants, so every
    existing caller that doesn't pass one gets byte-for-byte the same
    behavior as before per-merchant calibration existed."""
    recency_risk = min(100.0, (recency_days / calibration.recency_saturation_days) * 100.0)
    frequency_risk = max(0.0, 100.0 - (frequency / calibration.frequency_saturation) * 100.0)
    frequency_risk = min(100.0, frequency_risk)
    monetary_risk = max(0.0, 100.0 - (monetary / calibration.monetary_saturation) * 100.0)
    monetary_risk = min(100.0, monetary_risk)

    score = (
        WEIGHT_RECENCY * recency_risk
        + WEIGHT_FREQUENCY * frequency_risk
        + WEIGHT_MONETARY * monetary_risk
    )
    return round(max(0.0, min(100.0, score)), 2)


def compute_rfm(
    db: Session,
    member: Member,
    now: datetime | None = None,
    lookback_days: float = LOOKBACK_DAYS,
) -> tuple[float, int, float]:
    """Compute (recency_days, frequency, monetary) for a member.

    Frequency/monetary are counted over the trailing `lookback_days` window
    of *earn* transactions (purchases); recency is days since last activity
    of any kind (earn or redeem). Defaults to the original fixed
    LOOKBACK_DAYS when no per-merchant calibration is supplied.
    """
    now = now or datetime.now(timezone.utc)

    last_activity = member.last_activity_at
    if last_activity.tzinfo is None:
        last_activity = last_activity.replace(tzinfo=timezone.utc)
    recency_days = max(0.0, (now - last_activity).total_seconds() / 86400.0)

    window_start = now.timestamp() - lookback_days * 86400.0

    earn_txns = [
        t
        for t in member.transactions
        if t.type == TransactionType.EARN.value
        and (
            t.created_at.replace(tzinfo=timezone.utc) if t.created_at.tzinfo is None else t.created_at
        ).timestamp()
        >= window_start
    ]
    frequency = len(earn_txns)
    monetary = sum(t.amount_gbp for t in earn_txns)

    return recency_days, frequency, monetary


def score_member_churn(
    db: Session,
    member: Member,
    now: datetime | None = None,
    calibration: MerchantCalibration = DEFAULT_CALIBRATION,
) -> ChurnResult:
    recency_days, frequency, monetary = compute_rfm(
        db, member, now=now, lookback_days=calibration.lookback_days
    )
    score = churn_risk_from_rfm(recency_days, frequency, monetary, calibration=calibration)
    return ChurnResult(
        member_id=member.id,
        first_name=member.first_name,
        last_name=member.last_name,
        recency_days=round(recency_days, 1),
        frequency=frequency,
        monetary=round(monetary, 2),
        churn_risk_score=score,
        risk_band=_risk_band(score),
    )


def score_all_members(
    db: Session,
    merchant_id: str,
    now: datetime | None = None,
    calibration: MerchantCalibration | None = None,
) -> list[ChurnResult]:
    """Batch entry point -- this is what API call sites should use for a
    member list, so calibration is derived once per request rather than
    once per member. Auto-calibrates from this merchant's own data when no
    calibration is explicitly passed (the production default); pass one
    explicitly to reuse a calibration already computed elsewhere in the
    same request (e.g. by future_value's model training)."""
    if calibration is None:
        calibration = compute_merchant_calibration(db, merchant_id, now=now)
    members = db.query(Member).filter(Member.merchant_id == merchant_id).all()
    return [score_member_churn(db, m, now=now, calibration=calibration) for m in members]
