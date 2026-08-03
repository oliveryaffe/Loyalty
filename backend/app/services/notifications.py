"""Notification delivery (Slack/email) + churn-escalation transition
detection (PLAN_BATCH3.md §3).

No task queue exists in this codebase (see app/services/ledger.py /
app/services/billing.py for the same "MVP-scale, no extra infra" posture).
Rather than adding one, or sending synchronously inside a hot GET request
(a slow/down Slack endpoint would directly slow the merchant's dashboard
load), delivery uses FastAPI's built-in `BackgroundTasks` -- the HTTP
response returns immediately after the DB transition-detection below, and
the actual Slack POST / SMTP send happens after the response is sent.

`check_churn_escalations` is deliberately generic in naming/placement: it's
consumed both by this feature's own churn-notification path
(app/api/ai.py::get_churn_scores) and by §4's win-back automation
(app/services/winback.py::maybe_auto_trigger_winback), which needs the
exact same "who just crossed into high risk this request" signal. If this
feels mis-named once win-back is read alongside it, a trivial rename to
app/services/churn_triggers.py is a non-functional cleanup, not a redesign
(flagged the same way in the plan).
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx
from fastapi import BackgroundTasks
from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.ai.churn_model import ChurnResult
from app.config import settings
from app.db.models import Member, Merchant

logger = logging.getLogger(__name__)

# Batching (the other half of anti-spam, PLAN_BATCH3.md §3): one
# notification message per *triggering request*, not one per member/alert.
MAX_ITEMS_PER_NOTIFICATION = 10


def wants_churn_notifications(merchant: Merchant) -> bool:
    """`notify_on_churn_risk` is a nullable boolean (existing merchant rows
    read back as NULL after the additive ALTER) -- NULL is treated the same
    as True ("on" by default) so a merchant who never touched the settings
    page still gets notified once they configure a Slack URL/email."""
    return merchant.notify_on_churn_risk is not False


def wants_fraud_notifications(merchant: Merchant) -> bool:
    return merchant.notify_on_fraud_alert is not False


def send_slack(webhook_url: str, text: str) -> None:
    """POSTs {"text": text} via httpx with a short timeout. Catches and logs
    (logger.warning) all exceptions -- a Slack outage must never surface as
    an error to the merchant, since this always runs as a background task
    after the response has already gone out."""
    try:
        response = httpx.post(
            webhook_url,
            json={"text": text},
            timeout=settings.notification_http_timeout_seconds,
        )
        response.raise_for_status()
    except Exception:
        logger.warning("Slack notification failed (webhook_url=%s)", webhook_url, exc_info=True)


def send_email(to_address: str, subject: str, body: str) -> None:
    """smtplib against settings.smtp_host/port/username/password. No-ops
    with a logger.warning if smtp_host is unset (email sending "off" by
    default until the owner supplies SMTP credentials). Never raises --
    same reasoning as send_slack, this always runs as a background task."""
    if not settings.smtp_host:
        logger.warning(
            "Email notification skipped (smtp_host not configured): to=%s subject=%s",
            to_address,
            subject,
        )
        return

    try:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings.smtp_from_address
        message["To"] = to_address
        message.set_content(body)

        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=settings.notification_http_timeout_seconds
        ) as smtp:
            smtp.starttls()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except Exception:
        logger.warning("Email notification failed (to=%s subject=%s)", to_address, subject, exc_info=True)


def notify_merchant(merchant: Merchant, subject: str, body: str, background_tasks: BackgroundTasks) -> None:
    """Fans out to whichever of Slack/email the merchant configured, each as
    its own background task. A no-op (never an error, never blocks the
    response) if neither is configured."""
    if merchant.notification_slack_webhook_url:
        background_tasks.add_task(
            send_slack, merchant.notification_slack_webhook_url, f"*{subject}*\n{body}"
        )
    if merchant.notification_email:
        background_tasks.add_task(send_email, merchant.notification_email, subject, body)


def format_member_bullet_list(names: list[str]) -> str:
    """Renders a capped bullet list for a batched notification body --
    first MAX_ITEMS_PER_NOTIFICATION names, "+N more" suffix beyond that."""
    shown = names[:MAX_ITEMS_PER_NOTIFICATION]
    lines = [f"- {name}" for name in shown]
    remaining = len(names) - len(shown)
    if remaining > 0:
        lines.append(f"...and {remaining} more")
    return "\n".join(lines)


def check_churn_escalations(db: Session, merchant: Merchant, results: list[ChurnResult]) -> list[Member]:
    """Transition detection: a member's churn risk band newly escalating to
    "high" (was low/medium, now high) -- not "is currently high" (which
    would re-fire on every dashboard refresh).

    Reuses this codebase's own established concurrency pattern -- the exact
    atomic `UPDATE ... WHERE` shape app/services/ledger.py uses for balance
    changes -- rather than a naive Python-level "if band == high and last
    != high" check-then-write (a TOCTOU race). Only one concurrent request
    can ever flip a given member's row (the WHERE clause guarantees it), so
    "this request's UPDATE affected exactly one row" is the actual signal
    that this request is the one that should notify, not a re-read.

    For members whose *current* band is not "high", a plain (non-
    conditional) update keeps `last_known_risk_band` in sync so a future
    escalation is correctly detected as a transition.

    Pure DB-state function -- does not itself send anything (flushes but
    does not commit; the caller is responsible for committing alongside
    whatever else it does in the same request, e.g. app/api/ai.py). Returns
    members that just escalated to high risk *and* haven't been notified
    within the cooldown window (settings.notification_cooldown_hours,
    default 24) -- callers (this feature's own notify_merchant call, and
    §4's win-back auto-trigger) decide what to do with the returned list.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.notification_cooldown_hours)
    now = datetime.now(timezone.utc)

    escalated: list[Member] = []

    for result in results:
        if result.risk_band == "high":
            # `synchronize_session=False`: this WHERE clause compares a
            # datetime column, and SQLite round-trips DateTime(timezone=True)
            # values as tz-naive (see app/api/billing.py's tests for the same
            # observation) -- SQLAlchemy's default "evaluate" sync strategy
            # would try to re-check this WHERE clause in Python against the
            # already-loaded, possibly-tz-aware in-memory Member object and
            # raise `TypeError: can't compare offset-naive and offset-aware
            # datetimes`. Pushing the WHERE clause to the database (a plain
            # SQL UPDATE, evaluated entirely DB-side, same as
            # app/services/ledger.py's atomic updates) sidesteps that
            # entirely; we don't need Python-side session sync here since we
            # explicitly re-fetch via `db.get()` below for the members we
            # actually need.
            db_result = db.execute(
                update(Member)
                .where(
                    Member.id == result.member_id,
                    or_(
                        Member.last_known_risk_band.is_(None),
                        Member.last_known_risk_band != "high",
                        Member.risk_escalated_notified_at < cutoff,
                    ),
                )
                .values(last_known_risk_band="high", risk_escalated_notified_at=now)
                .execution_options(synchronize_session=False)
            )
            if db_result.rowcount == 1:
                member = db.get(Member, result.member_id)
                if member is not None:
                    escalated.append(member)
        else:
            db.execute(
                update(Member)
                .where(Member.id == result.member_id)
                .values(last_known_risk_band=result.risk_band)
                .execution_options(synchronize_session=False)
            )

    db.flush()
    # `db.get()` above may have returned identity-mapped objects with
    # stale in-memory attributes (synchronize_session=False intentionally
    # skips updating them) -- expire them so any subsequent attribute
    # access (e.g. app/api/ai.py formatting the notification body) reflects
    # what was just written, not a pre-update snapshot.
    for member in escalated:
        db.expire(member)
    return escalated
