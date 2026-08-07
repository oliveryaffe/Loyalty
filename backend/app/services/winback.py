"""Win-back worklist -- REWORKED from an auto-executing campaign feature
into a read-only, computed-on-demand suggestion list.

Why: the original version (`grant_winback_reward`, still visible in git
history) auto-granted a free, completed `Redemption` row the moment a
member's churn risk crossed a threshold. That only makes sense if Ledgerly
is the merchant's system of record for loyalty redemptions -- it isn't
(see the repositioning: Ledgerly is a predictive-insights layer that sits
on top of whatever POS/loyalty tool a merchant already runs, e.g. Square,
Loyalzoo, Stamp Me, or a plain CSV export). A "completed" redemption that
only exists in Ledgerly's database -- one the merchant's real POS and the
customer never saw -- was actively misleading, not a convenience.

`get_winback_worklist()` only reads (via `score_all_members`, unchanged)
and returns suggestions. Nothing is granted, nothing is written, nothing
is persisted. The merchant decides whether and how to act -- comping a
reward in whatever tool they already use for that.

`send_winback_email()` (added later, same file) is the one deliberate
exception to "nothing is written/sent" above, and it's worth being
precise about why it doesn't reopen the problem this module was reworked
to avoid: it never runs on its own, only when a merchant explicitly
clicks "send" for one specific customer, right now -- there is no
schedule, no drip sequence, no stored campaign, and no claim that
Ledgerly is a system of record for merchant/customer communication. It
exists because a merchant with no CRM or email tool of their own
otherwise has an at-risk-customer list with no way to act on it besides
waiting for that person to walk back in on their own. See
app/services/notifications.py::send_email for the actual SMTP send this
reuses (the same delivery path already used for owner-facing alerts).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.ai.churn_model import score_all_members
from app.config import settings
from app.db.models import Member, Merchant, RewardCatalogItem, WinbackRule
from app.services.notifications import send_email

DEFAULT_THRESHOLD = 65.0
MAX_WORKLIST_SIZE = 100

# How long after one win-back email a merchant must wait before Ledgerly
# will send another to the *same* customer. Deliberately longer than the
# 24h owner-notification cooldown (settings.notification_cooldown_hours)
# -- that one protects the merchant's own inbox from noise, this one
# protects an actual customer from getting emailed every time the
# merchant happens to reopen this member's card. A week is generous
# enough that a merchant testing the button twice in a row doesn't get a
# confusing false "sent" the second time, without meaningfully slowing
# down genuine, considered outreach.
WINBACK_EMAIL_COOLDOWN_HOURS = 24 * 7


@dataclass(frozen=True)
class WinbackWorklistEntry:
    member_id: str
    first_name: str
    last_name: str
    churn_risk_score: float
    risk_band: str
    suggested_reward_id: str | None
    suggested_reward_name: str | None


def get_suggested_winback_reward(db: Session, merchant: Merchant) -> RewardCatalogItem | None:
    """The merchant's saved win-back reward suggestion, if any -- shared
    by get_winback_worklist below and, separately, the churn-escalation
    notification (app/api/ai.py) so the same suggestion appears whether a
    merchant checks the worklist or gets pinged on Slack/email. Still
    read-only/suggestion-only: nothing here grants or sends anything, see
    module docstring."""
    rule = db.query(WinbackRule).filter(WinbackRule.merchant_id == merchant.id).first()
    if rule is None or not rule.reward_id:
        return None
    return db.get(RewardCatalogItem, rule.reward_id)


def get_winback_worklist(db: Session, merchant: Merchant) -> list[WinbackWorklistEntry]:
    """Members at or above the merchant's saved churn-risk threshold (or
    DEFAULT_THRESHOLD if no rule has been saved yet), highest risk first,
    capped at MAX_WORKLIST_SIZE. Recomputed from scratch on every call --
    the same score_all_members() pass GET /ai/churn already pays for, so
    this stays cheap and always current instead of drifting from an
    audit-trail table.

    `suggested_reward_*` is populated only when the merchant has saved a
    rule with a reward selected -- that reward is a *suggestion* for what
    to offer, mirrored from whatever catalog the merchant already runs
    elsewhere (see Rewards page copy). With no rule saved, the worklist
    still surfaces who's at risk, just with no suggested action attached.
    """
    rule = db.query(WinbackRule).filter(WinbackRule.merchant_id == merchant.id).first()
    threshold = rule.churn_risk_threshold if rule is not None else DEFAULT_THRESHOLD

    reward = get_suggested_winback_reward(db, merchant)

    results = score_all_members(db, merchant.id)
    at_risk = [r for r in results if r.churn_risk_score >= threshold]
    at_risk.sort(key=lambda r: r.churn_risk_score, reverse=True)

    return [
        WinbackWorklistEntry(
            member_id=r.member_id,
            first_name=r.first_name,
            last_name=r.last_name,
            churn_risk_score=r.churn_risk_score,
            risk_band=r.risk_band,
            suggested_reward_id=reward.id if reward is not None else None,
            suggested_reward_name=reward.name if reward is not None else None,
        )
        for r in at_risk[:MAX_WORKLIST_SIZE]
    ]


@dataclass(frozen=True)
class WinbackEmailResult:
    sent: bool
    # "sent" | "cooldown" | "smtp_not_configured" | "send_failed"
    reason: str
    cooldown_until: datetime | None = None


def _build_winback_email(
    merchant: Merchant, member: Member, reward: RewardCatalogItem | None
) -> tuple[str, str]:
    """Returns (subject, body). Deliberately plain-text and short -- this
    is a one-off personal check-in from a small business, not a
    templated marketing blast, and shouldn't read like one. Mentions the
    merchant's saved reward suggestion (if any) the same way the
    escalation alert and worklist do, so the message is consistent no
    matter which surface the merchant acted from."""
    subject = f"We miss you at {merchant.business_name}"
    lines = [
        f"Hi {member.first_name},",
        "",
        f"It's been a little while since your last visit to {merchant.business_name} -- "
        "we wanted to check in.",
    ]
    if reward is not None:
        lines += ["", f"Next time you're in, ask about {reward.name} -- on us."]
    lines += [
        "",
        "Hope to see you again soon,",
        merchant.business_name,
        "",
        "(If you'd rather not hear from us again, just reply and let us know.)",
    ]
    return subject, "\n".join(lines)


def _aware(dt: datetime) -> datetime:
    """SQLite round-trips DateTime(timezone=True) values as tz-naive (see
    app/ai/next_visit.py's identical helper, and app/services/notifications
    .py's cooldown-comparison comment for the same observation elsewhere in
    this codebase) -- Postgres doesn't have this problem, but treating a
    naive value read back from either backend as UTC keeps the comparison
    below correct regardless of which one is in play."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def send_winback_email(db: Session, merchant: Merchant, member: Member) -> WinbackEmailResult:
    """Manual, merchant-triggered single email to one at-risk customer --
    see module docstring for why this is the one write/send this module
    performs. Reuses whatever reward the merchant has saved via the
    win-back rule (get_suggested_winback_reward above), same as the
    worklist and the churn-escalation alert, so the offer is consistent
    everywhere it's mentioned.

    Cooldown is checked first and cheaply (no email built/sent) so a
    merchant re-clicking "send" on the same customer gets an immediate,
    clear "you already emailed them, wait until <date>" rather than a
    silent no-op or a duplicate send. The cooldown clock is only ever
    set on an actual successful send -- a skipped send (SMTP not
    configured) or a failed one doesn't start the clock, so fixing the
    underlying problem and trying again isn't blocked by this function's
    own bookkeeping.
    """
    now = datetime.now(timezone.utc)

    if member.last_winback_email_sent_at is not None:
        cooldown_until = _aware(member.last_winback_email_sent_at) + timedelta(hours=WINBACK_EMAIL_COOLDOWN_HOURS)
        if now < cooldown_until:
            return WinbackEmailResult(sent=False, reason="cooldown", cooldown_until=cooldown_until)

    reward = get_suggested_winback_reward(db, merchant)
    subject, body = _build_winback_email(merchant, member, reward)

    sent = send_email(
        member.email,
        subject,
        body,
        from_display_name=merchant.business_name,
        reply_to=merchant.notification_email,
    )
    if not sent:
        reason = "smtp_not_configured" if not settings.smtp_host else "send_failed"
        return WinbackEmailResult(sent=False, reason=reason)

    member.last_winback_email_sent_at = now
    db.commit()
    return WinbackEmailResult(sent=True, reason="sent")
