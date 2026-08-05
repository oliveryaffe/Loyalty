"""Weekly passive insight digest (competitive-brief backlog item #2).

Computes a short, plain-language summary of a merchant's account -- who's
newly at risk, what the book is worth going forward, and the single best
thing to do about it this week -- and formats it for delivery through the
existing Slack/email channels (app/services/notifications.py). Reuses the
same churn/future-value/recommendation models the dashboard itself calls;
this module adds no new modeling, only a different (summarized, pushed
rather than pulled) presentation of numbers that already exist elsewhere
in the product.

No task queue or scheduler exists in this codebase (see app/api/ai.py's
module docstring for the established rationale). Sending piggybacks on
GET /ai/churn -- the endpoint the dashboard already calls on every load --
via `maybe_send_weekly_digest`, called after that endpoint's own churn-
escalation check. A merchant with the digest turned on will therefore get
it fired automatically the next time anyone (them, a teammate, or a demo)
loads the dashboard after 7+ days have passed, with no cron job or extra
infrastructure required. `POST /digest/send` (app/api/digest.py) is the
same computation triggered on demand instead, e.g. a "send me a test"
button in Settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.ai.churn_model import score_all_members
from app.ai.future_value import score_all_members_future_value
from app.ai.recommender import recommend_for_member
from app.db.models import Member, Merchant

DIGEST_INTERVAL_DAYS = 7
MAX_AT_RISK_SHOWN = 5


def wants_weekly_digest(merchant: Merchant) -> bool:
    """Opt-in (unlike wants_churn_notifications/wants_fraud_notifications):
    NULL and False both mean off. See Merchant.notify_weekly_digest's
    docstring in app/db/models.py for why this toggle's default direction
    is deliberately the opposite of the other two."""
    return merchant.notify_weekly_digest is True


def is_digest_due(merchant: Merchant, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if merchant.last_digest_sent_at is None:
        return True
    last_sent = merchant.last_digest_sent_at
    if last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=timezone.utc)
    return now - last_sent >= timedelta(days=DIGEST_INTERVAL_DAYS)


@dataclass(frozen=True)
class DigestAtRiskMember:
    member_id: str
    name: str
    recency_days: float


@dataclass(frozen=True)
class WeeklyDigest:
    generated_at: datetime
    total_members: int
    at_risk_count: int
    at_risk_members: list[DigestAtRiskMember]
    predicted_value_90d: float
    top_opportunity: str
    headline: str


def compute_weekly_digest(db: Session, merchant: Merchant) -> WeeklyDigest:
    now = datetime.now(timezone.utc)
    churn_results = score_all_members(db, merchant.id, now=now)
    total_members = len(churn_results)

    high_risk = sorted(
        (r for r in churn_results if r.risk_band == "high"),
        key=lambda r: r.churn_risk_score,
        reverse=True,
    )
    at_risk_members = [
        DigestAtRiskMember(
            member_id=r.member_id,
            name=f"{r.first_name} {r.last_name}",
            recency_days=round(r.recency_days, 1),
        )
        for r in high_risk[:MAX_AT_RISK_SHOWN]
    ]

    future_value_results = score_all_members_future_value(db, merchant.id)
    predicted_value_90d = round(sum(r.predicted_value for r in future_value_results), 2)

    top_opportunity = _describe_top_opportunity(db, merchant, future_value_results)

    if not at_risk_members:
        headline = (
            f"No members are currently at high risk of churning across your {total_members} tracked "
            f"member(s) -- a quiet week."
        )
    elif len(at_risk_members) == 1:
        headline = f"1 member is at high risk of churning out of {total_members} tracked."
    else:
        headline = f"{len(at_risk_members)} members are at high risk of churning out of {total_members} tracked."

    return WeeklyDigest(
        generated_at=now,
        total_members=total_members,
        at_risk_count=len(high_risk),
        at_risk_members=at_risk_members,
        predicted_value_90d=predicted_value_90d,
        top_opportunity=top_opportunity,
        headline=headline,
    )


def _describe_top_opportunity(db: Session, merchant: Merchant, future_value_results: list) -> str:
    """One plain sentence naming the single highest-predicted-value member
    and, if a reward recommendation exists for them, what to offer them --
    the "here's your one biggest opportunity this week" line from the
    competitive brief. Falls back to a generic sentence if there are no
    members or no reward catalog yet, rather than leaving this blank."""
    if not future_value_results:
        return "No customer data yet -- upload a CSV or load sample data to see your first digest."

    top = max(future_value_results, key=lambda r: r.predicted_value)
    name = f"{top.first_name} {top.last_name}"
    base = f"{name} has your highest predicted value (£{top.predicted_value:.2f} over the next 90 days)."

    member = db.query(Member).filter(Member.id == top.member_id, Member.merchant_id == merchant.id).first()
    if member is None:
        return base

    ranked = recommend_for_member(db, member, top_n=1)
    if not ranked:
        return base

    best = ranked[0]
    return f"{base} Worth proactively offering them '{best.reward.name}' -- {best.reason}"


def format_digest_email(digest: WeeklyDigest, merchant_name: str) -> tuple[str, str]:
    """Renders (subject, plain-text body) for delivery via
    app/services/notifications.py::notify_merchant."""
    subject = f"Your weekly Ledgerly digest -- {digest.headline}"

    lines = [
        f"Weekly digest for {merchant_name}",
        "",
        digest.headline,
        "",
    ]
    if digest.at_risk_members:
        lines.append("At risk of churning:")
        for m in digest.at_risk_members:
            lines.append(f"- {m.name} ({m.recency_days:.0f} days since last visit)")
        if digest.at_risk_count > len(digest.at_risk_members):
            lines.append(f"...and {digest.at_risk_count - len(digest.at_risk_members)} more")
        lines.append("")

    lines.append(f"Predicted revenue from your current members over the next 90 days: £{digest.predicted_value_90d:.2f}")
    lines.append("")
    lines.append(f"This week's biggest opportunity: {digest.top_opportunity}")
    lines.append("")
    lines.append("-- Ledgerly")

    return subject, "\n".join(lines)
