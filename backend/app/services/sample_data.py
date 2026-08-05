"""Per-vertical synthetic sample data, generated on demand for a merchant
during onboarding ("load sample data for my business type").

This is deliberately NOT the same thing as backend/scripts/seed_data.py,
which seeds one fixed "Northwind Coffee Co." demo merchant used for the
hosted product demo/sales account. This module instead lets *any* merchant
generate a realistic starter dataset for their own account, scoped to
whichever business type they picked in onboarding -- so switching business
type in the demo actually changes what you see (different products,
different visit cadence, different basket sizes, a different starter
reward catalog), not just an invisible calibration constant.

Safety: sample data may only be generated for a merchant with zero real
data (see `has_real_data`). This is enforced server-side in
app/api/insights.py's endpoint, not just hidden in the UI -- a merchant's
actual uploaded/API-created transaction history must never be silently
replaced by fake data. Every row this module creates is flagged
`is_sample=True` (Member, RewardCatalogItem) so it can be cleanly cleared
and regenerated (e.g. re-running onboarding and picking a different
vertical) without ever touching real data, and so the frontend can show a
"you're viewing sample data" indicator.

Each vertical gets its own behavioral profile -- not just different
product names, but different visit cadence, basket size, and day-of-week/
seasonal weighting -- so churn risk and future-value scoring actually look
and behave differently per business type, not just cosmetically:

  - coffee_shop: frequent, low basket, mild weekday (commuter) bias.
  - restaurant: weekly-ish, larger basket, sharp Friday/Saturday spike.
  - barber_salon: appointment-cadence (~4-6 weeks), closed-Sunday,
    Saturday-heavy.
  - retail: sparse, largest basket, Nov/Dec seasonal surge + Saturday bump.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from faker import Faker
from sqlalchemy.orm import Session

from app.db.models import FraudAlert, Member, Merchant, RewardCatalogItem, Transaction, TransactionType

DEFAULT_MEMBER_COUNT = 110

_MEMBER_EMAIL_DOMAINS = ["gmail.com", "outlook.com", "hotmail.co.uk", "icloud.com", "yahoo.co.uk"]

# Same five behavioral cohorts backend/scripts/seed_data.py uses, same
# weights -- duplicated locally rather than imported, since scripts/ is a
# standalone entry point (its own sys.path bootstrap), not a library this
# package depends on.
_COHORT_WEIGHTS = [
    ("loyal", 0.15),
    ("lapsing", 0.15),
    ("at_risk", 0.15),
    ("new_member", 0.03),
    ("average", 0.52),
]


@dataclass(frozen=True)
class CohortPlan:
    num_txns: tuple[int, int]
    last_activity_days_ago: tuple[int, int]
    span_days: int
    amount_range: tuple[float, float]


@dataclass(frozen=True)
class VerticalProfile:
    business_type: str
    products: list[tuple[str, str]]  # (category, product_name)
    channels: list[tuple[str, float]]  # (channel, weight)
    cohort_plans: dict[str, CohortPlan]
    day_weight: Callable[[datetime], float]
    rewards: list[tuple[str, str, int, str]]  # (name, category, points_cost, tier_required)


def _coffee_day_weight(d: datetime) -> float:
    # Mild weekday (commuter) bias -- a coffee shop is busy Mon-Fri, just
    # somewhat quieter, not closed, on weekends.
    return 1.15 if d.weekday() < 5 else 0.85


def _restaurant_day_weight(d: datetime) -> float:
    wd = d.weekday()
    if wd in (4, 5):  # Fri, Sat
        return 2.4
    if wd == 6:  # Sun
        return 1.3
    return 0.55


def _barber_day_weight(d: datetime) -> float:
    wd = d.weekday()
    if wd == 6:  # closed Sundays -- a common independent-barbershop pattern
        return 0.05
    if wd == 5:  # Saturday is the busiest appointment day
        return 1.8
    return 1.0


def _retail_day_weight(d: datetime) -> float:
    month_mult = 2.0 if d.month in (11, 12) else (0.6 if d.month == 1 else 1.0)
    day_mult = 1.4 if d.weekday() == 5 else 1.0
    return month_mult * day_mult


VERTICAL_PROFILES: dict[str, VerticalProfile] = {
    "coffee_shop": VerticalProfile(
        business_type="coffee_shop",
        products=[
            ("beverage", "Flat White"),
            ("beverage", "Cold Brew"),
            ("beverage", "Oat Latte"),
            ("beverage", "Filter Coffee"),
            ("bakery", "Croissant"),
            ("bakery", "Cinnamon Bun"),
            ("bakery", "Blueberry Muffin"),
            ("food", "Breakfast Sandwich"),
        ],
        channels=[("pos", 0.55), ("mobile", 0.30), ("online", 0.15)],
        cohort_plans={
            "loyal": CohortPlan((18, 35), (0, 6), 150, (2.80, 12.50)),
            "lapsing": CohortPlan((2, 6), (95, 220), 60, (2.50, 9.50)),
            "at_risk": CohortPlan((4, 9), (45, 80), 90, (2.50, 9.50)),
            "new_member": CohortPlan((1, 3), (0, 15), 20, (2.50, 8.50)),
            "average": CohortPlan((6, 16), (3, 40), 150, (2.50, 11.00)),
        },
        day_weight=_coffee_day_weight,
        rewards=[
            ("Free Coffee", "beverage", 25, "bronze"),
            ("Free Pastry", "bakery", 40, "bronze"),
            ("£5 Cafe Credit", "gift-card", 50, "bronze"),
            ("£10 Cafe Credit", "gift-card", 100, "bronze"),
            ("Branded Keep Cup", "merchandise", 90, "silver"),
            ("Birthday Free Drink", "bonus", 0, "bronze"),
            ("Double Points Weekend Pass", "bonus", 150, "silver"),
            ("£25 Cafe Credit", "gift-card", 250, "gold"),
        ],
    ),
    "restaurant": VerticalProfile(
        business_type="restaurant",
        products=[
            ("mains", "Sunday Roast"),
            ("mains", "Burger & Chips"),
            ("mains", "Pasta Special"),
            ("mains", "Fish & Chips"),
            ("starters", "Soup of the Day"),
            ("starters", "Garlic Bread"),
            ("desserts", "Sticky Toffee Pudding"),
            ("drinks", "House Wine"),
        ],
        channels=[("pos", 0.65), ("online", 0.30), ("mobile", 0.05)],
        cohort_plans={
            "loyal": CohortPlan((8, 16), (0, 10), 150, (18.00, 65.00)),
            "lapsing": CohortPlan((2, 4), (100, 220), 60, (18.00, 55.00)),
            "at_risk": CohortPlan((3, 6), (50, 85), 90, (18.00, 55.00)),
            "new_member": CohortPlan((1, 2), (0, 15), 20, (18.00, 50.00)),
            "average": CohortPlan((3, 8), (5, 45), 150, (18.00, 55.00)),
        },
        day_weight=_restaurant_day_weight,
        rewards=[
            ("Free Starter", "starters", 60, "bronze"),
            ("Free Dessert", "desserts", 70, "bronze"),
            ("£10 Dining Credit", "gift-card", 100, "bronze"),
            ("Free Bottle of House Wine", "drinks", 150, "silver"),
            ("£25 Dining Credit", "gift-card", 250, "silver"),
            ("Birthday Meal for Two", "bonus", 0, "bronze"),
            ("Priority Weekend Booking", "perk", 120, "silver"),
            ("£50 Dining Credit", "gift-card", 500, "gold"),
        ],
    ),
    "barber_salon": VerticalProfile(
        business_type="barber_salon",
        products=[
            ("haircut", "Classic Cut"),
            ("haircut", "Skin Fade"),
            ("haircut", "Kids Cut"),
            ("beard", "Beard Trim"),
            ("colour", "Grey Blending"),
            ("treatment", "Hot Towel Shave"),
            ("treatment", "Scalp Treatment"),
        ],
        channels=[("pos", 0.90), ("online", 0.10)],
        cohort_plans={
            "loyal": CohortPlan((10, 16), (0, 20), 330, (18.00, 45.00)),
            "lapsing": CohortPlan((2, 3), (130, 260), 90, (18.00, 40.00)),
            "at_risk": CohortPlan((3, 5), (60, 100), 150, (18.00, 40.00)),
            "new_member": CohortPlan((1, 2), (0, 20), 25, (18.00, 38.00)),
            "average": CohortPlan((5, 9), (10, 55), 240, (18.00, 42.00)),
        },
        day_weight=_barber_day_weight,
        rewards=[
            ("Free Beard Trim", "beard", 40, "bronze"),
            ("£5 Off Next Cut", "gift-card", 50, "bronze"),
            ("Free Hot Towel Upgrade", "treatment", 60, "bronze"),
            ("Free Haircut (Every 6th)", "haircut", 220, "silver"),
            ("Referral Credit", "gift-card", 80, "bronze"),
            ("Birthday Free Treatment", "bonus", 0, "bronze"),
            ("VIP Grooming Kit", "merchandise", 300, "gold"),
        ],
    ),
    "retail": VerticalProfile(
        business_type="retail",
        products=[
            ("apparel", "Denim Jacket"),
            ("apparel", "Knit Jumper"),
            ("footwear", "Trainers"),
            ("footwear", "Ankle Boots"),
            ("accessories", "Leather Belt"),
            ("accessories", "Tote Bag"),
            ("homeware", "Candle Set"),
        ],
        channels=[("online", 0.50), ("pos", 0.40), ("mobile", 0.10)],
        cohort_plans={
            "loyal": CohortPlan((6, 14), (0, 25), 300, (20.00, 120.00)),
            "lapsing": CohortPlan((1, 3), (150, 300), 90, (20.00, 90.00)),
            "at_risk": CohortPlan((2, 4), (70, 140), 120, (20.00, 90.00)),
            "new_member": CohortPlan((1, 2), (0, 20), 25, (20.00, 80.00)),
            "average": CohortPlan((3, 7), (15, 70), 240, (20.00, 90.00)),
        },
        day_weight=_retail_day_weight,
        rewards=[
            ("10% Off Next Purchase", "gift-card", 60, "bronze"),
            ("Free Gift Wrapping", "perk", 30, "bronze"),
            ("£10 Store Credit", "gift-card", 100, "bronze"),
            ("£20 Store Credit", "gift-card", 200, "silver"),
            ("Early Access to Sale", "perk", 90, "silver"),
            ("Free Alterations", "perk", 80, "silver"),
            ("£50 Store Credit", "gift-card", 500, "gold"),
        ],
    ),
}

SAMPLE_DATA_BUSINESS_TYPES = tuple(VERTICAL_PROFILES.keys())


@dataclass(frozen=True)
class SampleDataResult:
    business_type: str
    members_created: int
    transactions_created: int
    rewards_created: int


def has_real_data(db: Session, merchant_id: str) -> bool:
    """True if this merchant has any Member row that ISN'T sample data --
    the hard gate on generate_sample_dataset. Uses `.is_not(True)` (not
    `== False`) so it correctly counts NULL the same as False -- rows
    written before is_sample existed, or by any path that doesn't set it
    explicitly, must be treated as real data, never silently skipped."""
    return (
        db.query(Member.id)
        .filter(Member.merchant_id == merchant_id, Member.is_sample.is_not(True))
        .first()
        is not None
    )


def is_viewing_sample_data(db: Session, merchant_id: str) -> bool:
    """True if this merchant currently has at least one member and none of
    them are real (i.e. everything on the account right now is sample
    data) -- powers the "you're viewing sample data" banner. A brand-new
    account with zero members yet is NOT "viewing sample data" (nothing to
    view), so this is deliberately not just `not has_real_data`."""
    if has_real_data(db, merchant_id):
        return False
    return db.query(Member.id).filter(Member.merchant_id == merchant_id).first() is not None


def clear_sample_data(db: Session, merchant: Merchant) -> None:
    """Removes any previously-generated sample Members (and their
    Transactions/Redemptions via cascade, plus FraudAlerts which aren't
    cascaded automatically) and sample RewardCatalogItems. Safe to call
    even if there's nothing to clear. Caller commits."""
    sample_members = (
        db.query(Member).filter(Member.merchant_id == merchant.id, Member.is_sample.is_(True)).all()
    )
    if sample_members:
        sample_member_ids = [m.id for m in sample_members]
        db.query(FraudAlert).filter(FraudAlert.member_id.in_(sample_member_ids)).delete(
            synchronize_session=False
        )
        for member in sample_members:
            db.delete(member)  # cascades transactions + redemptions

    db.query(RewardCatalogItem).filter(
        RewardCatalogItem.merchant_id == merchant.id, RewardCatalogItem.is_sample.is_(True)
    ).delete(synchronize_session=False)
    db.flush()


def _pick_cohort(rng: random.Random) -> str:
    r = rng.random()
    cumulative = 0.0
    for name, weight in _COHORT_WEIGHTS:
        cumulative += weight
        if r <= cumulative:
            return name
    return "average"


def _sample_email(first: str, last: str, rng: random.Random, seen_emails: set[str]) -> str:
    base = "".join(ch for ch in f"{first}.{last}".lower() if ch.isalnum() or ch == ".")
    domain = rng.choice(_MEMBER_EMAIL_DOMAINS)
    candidate = f"{base}@{domain}"
    suffix = 2
    while candidate in seen_emails:
        candidate = f"{base}{suffix}@{domain}"
        suffix += 1
    seen_emails.add(candidate)
    return candidate


def _build_member(fake: Faker, merchant: Merchant, cohort: str, rng: random.Random, seen_emails: set[str], now: datetime) -> Member:
    first = fake.first_name()
    last = fake.last_name()
    tier = rng.choices(["bronze", "silver", "gold", "platinum"], weights=[0.5, 0.3, 0.15, 0.05])[0]
    joined_days_ago = rng.randint(5, 40) if cohort == "new_member" else rng.randint(30, 500)
    return Member(
        merchant_id=merchant.id,
        first_name=first,
        last_name=last,
        email=_sample_email(first, last, rng, seen_emails),
        tier=tier,
        points_balance=0,
        is_active=True,
        synthetic_cohort=cohort,
        is_sample=True,
        joined_at=now - timedelta(days=joined_days_ago),
        last_activity_at=now,
    )


def _weighted_day_offsets(span_days: int, latest: datetime, day_weight: Callable[[datetime], float]) -> tuple[list[int], list[float]]:
    offsets = list(range(span_days + 1))
    weights = [day_weight(latest - timedelta(days=o)) for o in offsets]
    return offsets, weights


def _generate_transactions(
    member: Member, plan: CohortPlan, profile: VerticalProfile, rng: random.Random, now: datetime
) -> list[Transaction]:
    last_activity_days_ago = rng.randint(*plan.last_activity_days_ago)
    latest = now - timedelta(days=last_activity_days_ago, hours=rng.uniform(0, 23))
    num_txns = rng.randint(*plan.num_txns)
    if num_txns == 0:
        return []

    offsets, weights = _weighted_day_offsets(plan.span_days, latest, profile.day_weight)
    chosen_offsets = rng.choices(offsets, weights=weights, k=num_txns)

    channel_names = [c for c, _ in profile.channels]
    channel_weights = [w for _, w in profile.channels]

    txns = []
    for offset in chosen_offsets:
        created_at = latest - timedelta(days=offset, hours=rng.uniform(0, 23))
        amount = round(rng.uniform(*plan.amount_range), 2)
        points = int(amount)
        category, product_name = rng.choice(profile.products)
        channel = rng.choices(channel_names, weights=channel_weights, k=1)[0]
        txns.append(
            Transaction(
                member_id=member.id,
                type=TransactionType.EARN.value,
                amount_gbp=amount,
                points=points,
                channel=channel,
                product_category=category,
                product_name=product_name,
                created_at=created_at,
            )
        )
    txns.sort(key=lambda t: t.created_at)
    return txns


def _inject_amount_spike(
    member: Member, plan: CohortPlan, profile: VerticalProfile, rng: random.Random, now: datetime
) -> Transaction:
    spike_min = plan.amount_range[1] * 15
    spike_max = plan.amount_range[1] * 40
    amount = round(rng.uniform(spike_min, spike_max), 2)
    created_at = now - timedelta(days=rng.randint(1, 20), hours=rng.uniform(0, 23))
    category, product_name = rng.choice(profile.products)
    return Transaction(
        member_id=member.id,
        type=TransactionType.EARN.value,
        amount_gbp=amount,
        points=int(amount),
        channel="online",
        product_category=category,
        product_name=product_name,
        created_at=created_at,
        synthetic_fraud_label=True,
    )


def _inject_velocity_burst(
    member: Member, plan: CohortPlan, profile: VerticalProfile, rng: random.Random, now: datetime
) -> list[Transaction]:
    burst_start = now - timedelta(days=rng.randint(1, 15), hours=rng.uniform(0, 12))
    count = rng.randint(9, 14)
    txns = []
    for i in range(count):
        created_at = burst_start + timedelta(minutes=rng.uniform(0, 90) + i * 2)
        amount = round(rng.uniform(*plan.amount_range), 2)
        category, product_name = rng.choice(profile.products)
        txns.append(
            Transaction(
                member_id=member.id,
                type=TransactionType.EARN.value,
                amount_gbp=amount,
                points=int(amount),
                channel="online",
                product_category=category,
                product_name=product_name,
                created_at=created_at,
                synthetic_fraud_label=True,
            )
        )
    return txns


def generate_sample_dataset(
    db: Session, merchant: Merchant, business_type: str, member_count: int = DEFAULT_MEMBER_COUNT
) -> SampleDataResult:
    """Generates a fresh vertical-specific sample dataset for `merchant`.
    Raises ValueError if `business_type` isn't a known vertical, or if the
    merchant already has real (non-sample) data -- callers (see
    app/api/insights.py) should check `has_real_data` themselves first to
    return a proper 409 rather than relying on this exception."""
    if business_type not in VERTICAL_PROFILES:
        raise ValueError(f"no sample data profile for business_type={business_type!r}")
    if has_real_data(db, merchant.id):
        raise ValueError("merchant already has real data -- refusing to generate sample data over it")

    profile = VERTICAL_PROFILES[business_type]
    # Deliberately unseeded -- unlike backend/scripts/seed_data.py's fixed
    # SEED=42 (which needs to be reproducible for its own tests), sample
    # data should look slightly different each time it's (re)generated so
    # reloading it for a demo doesn't feel like the exact same fixture on
    # replay.
    rng = random.Random()
    fake = Faker()

    clear_sample_data(db, merchant)

    now = datetime.now(timezone.utc)
    seen_emails = {
        row[0] for row in db.query(Member.email).filter(Member.merchant_id == merchant.id).all()
    }

    members: list[Member] = []
    for _ in range(member_count):
        cohort = _pick_cohort(rng)
        member = _build_member(fake, merchant, cohort, rng, seen_emails, now)
        db.add(member)
        members.append(member)
    db.flush()

    fraud_pool = rng.sample(members, k=max(3, int(member_count * 0.03)))
    amount_spike_members = {m.id for m in fraud_pool[: len(fraud_pool) // 2]}
    velocity_members = {m.id for m in fraud_pool[len(fraud_pool) // 2 :]}

    total_txns = 0
    for member in members:
        plan = profile.cohort_plans[member.synthetic_cohort]
        txns = _generate_transactions(member, plan, profile, rng, now)
        if member.id in amount_spike_members:
            txns.append(_inject_amount_spike(member, plan, profile, rng, now))
        if member.id in velocity_members:
            txns.extend(_inject_velocity_burst(member, plan, profile, rng, now))
        txns.sort(key=lambda t: t.created_at)
        for t in txns:
            db.add(t)
        total_txns += len(txns)

        member.points_balance = sum(t.points for t in txns)
        member.last_activity_at = max((t.created_at for t in txns), default=member.joined_at)

    db.flush()

    rewards_created = 0
    for name, category, points_cost, tier in profile.rewards:
        db.add(
            RewardCatalogItem(
                merchant_id=merchant.id,
                name=name,
                description=f"{name} -- redeemable via loyalty points.",
                category=category,
                points_cost=points_cost,
                tier_required=tier,
                active=True,
                is_sample=True,
            )
        )
        rewards_created += 1
    db.flush()

    return SampleDataResult(
        business_type=business_type,
        members_created=len(members),
        transactions_created=total_txns,
        rewards_created=rewards_created,
    )
