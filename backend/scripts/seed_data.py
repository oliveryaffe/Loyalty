"""Synthetic data generator (PLAN.md P0 acceptance criterion).

Populates the DB with a demo merchant, >=500 members split across
realistic behavioral cohorts, and >=5,000 transactions -- including a
handful of intentionally injected fraud-like patterns (abnormal amount
spikes and abnormal earn velocity), each tagged with
`synthetic_fraud_label=True` and `synthetic_cohort` so tests can verify the
AI layer actually catches what it's supposed to.

Usage (from backend/):
    python scripts/seed_data.py [--reset]

Deterministic: seeded with a fixed RNG seed so runs (and the tests that
depend on the resulting data shape) are reproducible.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone

from faker import Faker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.base import Base, SessionLocal, engine, init_db  # noqa: E402
from app.db.models import (  # noqa: E402
    Member,
    Merchant,
    Redemption,
    RewardCatalogItem,
    TeamMember,
    TeamRole,
    Transaction,
    TransactionType,
)
from app.services.sample_data import VERTICAL_PROFILES  # noqa: E402
from app.services.security import hash_password  # noqa: E402

# The persistent demo account (seeded here) is coffee-shop-themed (see
# generate_member for the cohort/tier shape) -- reuse the same product
# catalog app/services/sample_data.py's in-app "Load sample data" flow
# uses for that vertical, rather than maintaining a second product list.
# This is also what fixes next_best_product.py's demo-account output:
# without product_category/product_name on transactions, NBP degrades to
# the sparse Redemption-based category fallback (every recommendation's
# product_name stays null -- see tests/test_next_best_product.py's
# category-fallback tests, now exercised against a dedicated minimal
# fixture instead of this seed script).
DEMO_PRODUCTS = VERTICAL_PROFILES["coffee_shop"].products

SEED = 42
NUM_MEMBERS = 620
DEMO_MERCHANT_EMAIL = "demo@merchant.com"
DEMO_MERCHANT_PASSWORD = "demo1234"
DEMO_TEAM_MEMBER_EMAIL = "demo-member@merchant.com"
DEMO_TEAM_MEMBER_PASSWORD = "demo1234"
DEMO_SHOPIFY_WEBHOOK_SECRET = "demo-shopify-secret-change-me"
DEMO_SHOPIFY_SHOP_DOMAIN = "northwind-coffee-demo.myshopify.com"

# Points costs rescaled to match realistic coffee-shop transaction sizes
# (points_per_pound=1.0, and a typical visit is ~£3-8, not the £15-90 the
# ranges originally assumed -- see cohort_purchase_plan's docstring). Free
# Coffee at 25 points now works out to roughly "every 5th coffee free",
# a believable independent-cafe loyalty offer instead of ~50 visits.
REWARD_CATALOG = [
    # (name, category, points_cost, tier_required)
    ("£5 Store Credit", "gift-card", 50, "bronze"),
    ("£10 Store Credit", "gift-card", 100, "bronze"),
    ("£25 Store Credit", "gift-card", 250, "silver"),
    ("£50 Store Credit", "gift-card", 500, "gold"),
    ("Free Coffee", "beverage", 25, "bronze"),
    ("Free Pastry", "beverage", 40, "bronze"),
    ("Branded Tote Bag", "apparel", 80, "bronze"),
    ("Branded T-Shirt", "apparel", 120, "silver"),
    ("Branded Hoodie", "apparel", 280, "silver"),
    ("Wireless Earbuds", "electronics", 450, "gold"),
    ("Bluetooth Speaker", "electronics", 350, "gold"),
    ("Smart Watch", "electronics", 900, "platinum"),
    ("VIP Early Access Event", "experience", 200, "silver"),
    ("Private Shopping Session", "experience", 600, "gold"),
    ("Birthday Bonus Points", "bonus", 0, "bronze"),
    ("Double Points Weekend Pass", "bonus", 150, "silver"),
    ("£100 Store Credit", "gift-card", 1000, "platinum"),
    ("Free Shipping (1 Year)", "perk", 300, "silver"),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def reset_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def make_rewards(db, merchant: Merchant) -> list[RewardCatalogItem]:
    rewards = []
    for name, category, points_cost, tier in REWARD_CATALOG:
        r = RewardCatalogItem(
            merchant_id=merchant.id,
            name=name,
            description=f"{name} -- redeemable via loyalty points.",
            category=category,
            points_cost=points_cost,
            tier_required=tier,
            active=True,
        )
        db.add(r)
        rewards.append(r)
    db.flush()
    return rewards


COHORT_WEIGHTS = [
    ("loyal", 0.15),
    ("lapsing", 0.15),
    ("at_risk", 0.15),
    ("new_member", 0.03),  # Batch 2: genuinely brand-new members, no activity older than ~35 days --
    # exercises app/ai/future_value.py's per-member heuristic fallback path (a member with zero
    # pre-cutoff earn activity relative to HOLDOUT_DAYS=45) out of the box, with no upload needed.
    # Carved out of "average" below (0.55 -> 0.52) so loyal/lapsing/at_risk (and their existing
    # test assertions in test_churn_model.py) are unaffected.
    ("average", 0.52),
]


def pick_cohort(rng: random.Random) -> str:
    r = rng.random()
    cumulative = 0.0
    for name, weight in COHORT_WEIGHTS:
        cumulative += weight
        if r <= cumulative:
            return name
    return "average"


def cohort_purchase_plan(cohort: str, rng: random.Random, now: datetime) -> tuple[int, int, int, float, float]:
    """Return (num_txns, min_days_ago_span_start, days_since_last_activity,
    min_amount, max_amount) shaping how a member's earn history looks.

    Amount ranges are calibrated for an independent coffee shop, not a
    generic retailer: a typical visit is a coffee (~£3) or coffee + pastry
    (~£5-8), occasionally a round for a small group (~£12-15). Values
    above ~£15 are the exception, not the norm -- previously these ranged
    up to £90 per transaction, which read as implausible for a single
    coffee-shop visit and inflated every downstream number that derives
    from amount_gbp (points balances, Future Value projections, avg order
    value on the dashboard)."""
    if cohort == "loyal":
        num_txns = rng.randint(18, 35)
        last_activity_days_ago = rng.randint(0, 6)
        span_days = 150
        amount_range = (2.80, 12.50)
    elif cohort == "lapsing":
        num_txns = rng.randint(2, 6)
        last_activity_days_ago = rng.randint(95, 220)
        span_days = 60  # all their activity clustered in an old window
        amount_range = (2.50, 9.50)
    elif cohort == "at_risk":
        num_txns = rng.randint(4, 9)
        last_activity_days_ago = rng.randint(45, 80)
        span_days = 90
        amount_range = (2.50, 9.50)
    elif cohort == "new_member":
        # All activity within the last ~35 days -- well inside any
        # reasonable backtest cutoff, so this cohort has zero pre-cutoff
        # earn history (see COHORT_WEIGHTS comment above).
        num_txns = rng.randint(1, 3)
        last_activity_days_ago = rng.randint(0, 15)
        span_days = 20
        amount_range = (2.50, 8.50)
    else:  # average
        num_txns = rng.randint(6, 16)
        last_activity_days_ago = rng.randint(3, 40)
        span_days = 150
        amount_range = (2.50, 11.00)

    return num_txns, last_activity_days_ago, span_days, amount_range[0], amount_range[1]


# Common UK-realistic personal email providers -- used instead of Faker's
# example.com/example.org/example.net defaults so seeded addresses read as
# real personal emails, not obviously-fake placeholders.
_MEMBER_EMAIL_DOMAINS = ["gmail.com", "outlook.com", "hotmail.co.uk", "icloud.com", "yahoo.co.uk"]


def _member_email(first: str, last: str, rng: random.Random, seen_emails: set[str]) -> str:
    """Deterministic, name-matching email -- previously this called
    fake.unique.email(), which generates a fully random address with zero
    connection to the member's actual name (confirmed live: e.g. a member
    named "Sarah Jenkins" showing up with an email like
    "xk29fhq3@example.net" on the Members page). Builds `first.last@domain`
    lowercased instead; a numeric suffix is only appended on an actual
    collision (two members sharing the same first+last name), so the
    common case still looks like a real personal email address."""
    base = "".join(ch for ch in f"{first}.{last}".lower() if ch.isalnum() or ch == ".")
    domain = rng.choice(_MEMBER_EMAIL_DOMAINS)
    candidate = f"{base}@{domain}"
    suffix = 2
    while candidate in seen_emails:
        candidate = f"{base}{suffix}@{domain}"
        suffix += 1
    seen_emails.add(candidate)
    return candidate


def build_member(
    fake: Faker, merchant: Merchant, cohort: str, rng: random.Random, seen_emails: set[str]
) -> Member:
    first = fake.first_name()
    last = fake.last_name()
    tier = random.choices(
        ["bronze", "silver", "gold", "platinum"], weights=[0.5, 0.3, 0.15, 0.05]
    )[0]
    # new_member cohort: joined recently too (not just "transacted
    # recently") -- consistent with the plan's "very recently joined
    # synthetic member" framing for the future-value heuristic fallback.
    joined_days_ago = random.randint(5, 40) if cohort == "new_member" else random.randint(30, 500)
    return Member(
        merchant_id=merchant.id,
        first_name=first,
        last_name=last,
        email=_member_email(first, last, rng, seen_emails),
        tier=tier,
        points_balance=0,
        synthetic_cohort=cohort,
        joined_at=utc_now() - timedelta(days=joined_days_ago),
        last_activity_at=utc_now(),
    )


def generate_earn_transactions(
    member: Member, cohort: str, rng: random.Random, now: datetime
) -> list[Transaction]:
    num_txns, last_activity_days_ago, span_days, min_amt, max_amt = cohort_purchase_plan(
        cohort, rng, now
    )
    latest = now - timedelta(days=last_activity_days_ago, hours=rng.randint(0, 23))
    txns = []
    for i in range(num_txns):
        offset_days = rng.uniform(0, span_days)
        created_at = latest - timedelta(days=offset_days, hours=rng.uniform(0, 23))
        amount = round(rng.uniform(min_amt, max_amt), 2)
        points = int(amount)  # 1:1 points_per_pound, floored
        category, product_name = rng.choice(DEMO_PRODUCTS)
        txn = Transaction(
            member_id=member.id,
            type=TransactionType.EARN.value,
            amount_gbp=amount,
            points=points,
            channel=rng.choice(["pos", "online", "mobile"]),
            created_at=created_at,
            product_category=category,
            product_name=product_name,
        )
        txns.append(txn)

    txns.sort(key=lambda t: t.created_at)
    return txns


def inject_amount_spike_fraud(member: Member, rng: random.Random, now: datetime) -> Transaction:
    """A single wildly-oversized purchase relative to the member's normal
    spend -- simulates a stolen-card / miskeyed-amount / gift-card-abuse
    style anomaly."""
    amount = round(rng.uniform(1200.0, 4000.0), 2)
    points = int(amount)
    created_at = now - timedelta(days=rng.randint(1, 20), hours=rng.uniform(0, 23))
    category, product_name = rng.choice(DEMO_PRODUCTS)
    return Transaction(
        member_id=member.id,
        type=TransactionType.EARN.value,
        amount_gbp=amount,
        points=points,
        channel="online",
        created_at=created_at,
        synthetic_fraud_label=True,
        product_category=category,
        product_name=product_name,
    )


def inject_velocity_burst_fraud(member: Member, rng: random.Random, now: datetime) -> list[Transaction]:
    """A burst of many small earn transactions packed into a short window --
    simulates points-farming / automated abuse."""
    burst_start = now - timedelta(days=rng.randint(1, 15), hours=rng.uniform(0, 12))
    count = rng.randint(9, 14)
    txns = []
    for i in range(count):
        created_at = burst_start + timedelta(minutes=rng.uniform(0, 90) + i * 2)
        # Ordinary-sized purchases -- the anomaly signal here is velocity
        # (many earn events packed into a short window), not amount, so
        # these should look like normal coffee-shop transactions.
        amount = round(rng.uniform(3.0, 12.0), 2)
        points = int(amount)
        category, product_name = rng.choice(DEMO_PRODUCTS)
        txns.append(
            Transaction(
                member_id=member.id,
                type=TransactionType.EARN.value,
                amount_gbp=amount,
                points=points,
                channel="online",
                created_at=created_at,
                synthetic_fraud_label=True,
                product_category=category,
                product_name=product_name,
            )
        )
    return txns


def seed(reset: bool = True) -> None:
    random.seed(SEED)
    rng = random.Random(SEED)
    fake = Faker()
    Faker.seed(SEED)

    if reset:
        print("Resetting database (drop_all + create_all)...")
        reset_db()
    else:
        # --no-reset: adding to a DB that may already exist with an older
        # schema -- use init_db() (create_all + column-rename/sync), not a
        # bare create_all, for the same reason seed_if_empty() does (see
        # its docstring): create_all alone never adds columns to a table
        # that already exists on disk.
        init_db()

    db = SessionLocal()
    now = utc_now()
    try:
        merchant = Merchant(
            business_name="Northwind Coffee Co.",
            business_type="coffee_shop",
            shopify_webhook_secret=DEMO_SHOPIFY_WEBHOOK_SECRET,
            shopify_shop_domain=DEMO_SHOPIFY_SHOP_DOMAIN,
            # Stripe billing (PLAN_BATCH3.md §2): the demo merchant must have
            # an active subscription, or every protected route (now gated by
            # require_active_subscription instead of get_current_merchant)
            # would 402 the moment this batch ships -- see PLAN_BATCH3.md's
            # "Risk to the existing 114 tests" table, flagged there as the
            # single highest-regression-risk item in the whole batch.
            # subscription_current_period_end is set far in the future so it
            # never looks "stale" in the dashboard's billing banner.
            subscription_status="active",
            subscription_tier="growth",
            subscription_current_period_end=now + timedelta(days=3650),
        )
        db.add(merchant)
        db.flush()

        admin_team_member = TeamMember(
            merchant_id=merchant.id,
            email=DEMO_MERCHANT_EMAIL,
            hashed_password=hash_password(DEMO_MERCHANT_PASSWORD),
            role=TeamRole.ADMIN.value,
        )
        member_team_member = TeamMember(
            merchant_id=merchant.id,
            email=DEMO_TEAM_MEMBER_EMAIL,
            hashed_password=hash_password(DEMO_TEAM_MEMBER_PASSWORD),
            role=TeamRole.MEMBER.value,
        )
        db.add(admin_team_member)
        db.add(member_team_member)
        db.flush()

        rewards = make_rewards(db, merchant)
        print(f"Created {len(rewards)} reward catalog items.")

        members: list[Member] = []
        seen_emails: set[str] = set()
        for _ in range(NUM_MEMBERS):
            cohort = pick_cohort(rng)
            member = build_member(fake, merchant, cohort, rng, seen_emails)
            db.add(member)
            members.append(member)
        db.flush()
        print(f"Created {len(members)} members.")

        # Pick a small set of members to receive injected fraud patterns.
        # ~2% amount-spike, ~1.5% velocity-burst (some overlap allowed).
        fraud_pool = rng.sample(members, k=max(10, int(NUM_MEMBERS * 0.035)))
        amount_spike_members = set(m.id for m in fraud_pool[: len(fraud_pool) // 2])
        velocity_burst_members = set(m.id for m in fraud_pool[len(fraud_pool) // 2 :])

        total_txns = 0
        injected_fraud_txns = 0
        all_txns_by_member: dict[str, list[Transaction]] = {}

        for member in members:
            txns = generate_earn_transactions(member, member.synthetic_cohort, rng, now)

            if member.id in amount_spike_members:
                txns.append(inject_amount_spike_fraud(member, rng, now))
                injected_fraud_txns += 1
            if member.id in velocity_burst_members:
                burst = inject_velocity_burst_fraud(member, rng, now)
                txns.extend(burst)
                injected_fraud_txns += len(burst)

            txns.sort(key=lambda t: t.created_at)
            for t in txns:
                db.add(t)
            all_txns_by_member[member.id] = txns
            total_txns += len(txns)

            balance = sum(t.points for t in txns)
            member.points_balance = balance
            if txns:
                latest_txn = max(txns, key=lambda t: t.created_at)
                member.last_activity_at = latest_txn.created_at
            else:
                member.last_activity_at = member.joined_at

        db.flush()
        print(f"Created {total_txns} transactions ({injected_fraud_txns} intentionally fraud-like).")

        # A handful of completed redemptions, for members who can afford at
        # least one reward -- gives the recommender category-affinity and
        # popularity signal to work with.
        redeemable_rewards = [r for r in rewards if r.points_cost > 0]
        redemption_count = 0
        for member in members:
            if member.points_balance <= 0:
                continue
            affordable = [
                r
                for r in redeemable_rewards
                if r.points_cost <= member.points_balance
                and _tier_ok(member.tier, r.tier_required)
            ]
            if not affordable or rng.random() > 0.35:
                continue
            reward = rng.choice(affordable)
            redemption_txn = Transaction(
                member_id=member.id,
                type=TransactionType.REDEEM.value,
                amount_gbp=0.0,
                points=-reward.points_cost,
                channel="redemption",
                created_at=now - timedelta(days=rng.randint(0, 60)),
            )
            db.add(redemption_txn)
            db.flush()
            member.points_balance -= reward.points_cost
            db.add(
                Redemption(
                    member_id=member.id,
                    reward_id=reward.id,
                    transaction_id=redemption_txn.id,
                    points_spent=reward.points_cost,
                    status="completed",
                    created_at=redemption_txn.created_at,
                )
            )
            redemption_count += 1

        db.commit()
        print(f"Created {redemption_count} completed redemptions.")

        print("\nDemo merchant admin login:")
        print(f"  email:    {DEMO_MERCHANT_EMAIL}")
        print(f"  password: {DEMO_MERCHANT_PASSWORD}")
        print("\nDemo merchant non-admin (role=member) login:")
        print(f"  email:    {DEMO_TEAM_MEMBER_EMAIL}")
        print(f"  password: {DEMO_TEAM_MEMBER_PASSWORD}")
        print("\nDemo Shopify webhook config (for scripts/send_sample_shopify_webhook.py):")
        print(f"  merchant_id: {merchant.id}")
        print(f"  secret:      {DEMO_SHOPIFY_WEBHOOK_SECRET}")
        print("\nSeed complete.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


_TIER_RANK = {"bronze": 0, "silver": 1, "gold": 2, "platinum": 3}


def _tier_ok(member_tier: str, required_tier: str) -> bool:
    return _TIER_RANK.get(member_tier, 0) >= _TIER_RANK.get(required_tier, 0)


def seed_if_empty() -> None:
    """Idempotent bootstrap mode for container startup (Feature 1): create
    any missing tables (non-destructive), then only run the full synthetic
    seed if the merchants table is empty. A no-op against a database that
    already has data -- this is what makes redeploys/restarts against a
    persistent Postgres volume safe (they no longer wipe real data), while
    still auto-seeding on a genuinely fresh/empty database.

    Uses `init_db()` (create_all + `_apply_column_renames` +
    `_sync_missing_columns`), NOT a bare `Base.metadata.create_all` --
    this script runs as a separate process *before* uvicorn/app startup
    in the Dockerfile's CMD (`seed_data.py --seed-if-empty && uvicorn
    ...`), so it's the only thing that touches the DB before the
    `db.query(Merchant).first()` below runs. A bare create_all only
    creates missing tables, it does NOT add new columns to a table that
    already exists on disk -- calling it here directly (as this
    function used to) crashed Batch 3's very first production deploy
    with `UndefinedColumn: merchants.stripe_customer_id does not
    exist`, because the schema-sync that would have added that column
    lives in `init_db()`, which only ever ran later, inside the FastAPI
    startup event -- too late for this script's own query.
    """
    init_db()
    db = SessionLocal()
    try:
        existing = db.query(Merchant).first()
    finally:
        db.close()

    if existing is not None:
        print("Database already has data (a Merchant row exists) -- --seed-if-empty is a no-op.")
        return

    print("Database is empty -- running initial synthetic seed...")
    seed(reset=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the loyalty DB with synthetic data.")
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Don't drop existing tables first (default is to reset for a clean, reproducible seed).",
    )
    parser.add_argument(
        "--seed-if-empty",
        action="store_true",
        help=(
            "Idempotent bootstrap mode: create missing tables and seed only if the "
            "merchants table is empty; otherwise no-op. Safe to run on every container "
            "start (see backend/Dockerfile / README Postgres section). Mutually exclusive "
            "in effect with --no-reset (this flag never drops existing tables)."
        ),
    )
    args = parser.parse_args()
    if args.seed_if_empty:
        seed_if_empty()
    else:
        seed(reset=not args.no_reset)
