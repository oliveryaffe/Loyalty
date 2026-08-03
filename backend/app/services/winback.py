"""Win-back campaign automation (PLAN_BATCH3.md §4): rule-driven auto-offer
for high-churn-risk members. One rule per merchant (MVP scope, not a
campaign builder) -- see app/db/models.py::WinbackRule.

Two trigger paths, both funneled through the same grant-and-record logic:

1. Manual, admin-initiated (`run_manual_winback`, called from
   `POST /api/v1/winback/run`).
2. Automatic, piggybacking on §3's escalation detection
   (`maybe_auto_trigger_winback`, called from
   `app/api/ai.py::get_churn_scores` right after
   `app/services/notifications.py::check_churn_escalations` -- this is the
   dependency the plan calls out explicitly: win-back's auto-trigger path
   only exists because §3 already computed "who just transitioned to high
   risk this request"). `auto_trigger` defaults to False on `WinbackRule`,
   so this path is a no-op for every merchant until they explicitly opt in.
"""
from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.churn_model import score_all_members
from app.db.models import (
    Member,
    Merchant,
    Redemption,
    RedemptionStatus,
    RewardCatalogItem,
    WinbackOffer,
    WinbackRule,
)

logger = logging.getLogger(__name__)


def grant_winback_reward(db: Session, member: Member, reward: RewardCatalogItem) -> Redemption:
    """Creates a completed Redemption with points_spent=0, source='winback'.

    Deliberately does NOT call app/services/ledger.py::redeem_reward() --
    skips the balance check, tier check, and points debit entirely (a
    win-back offer exists specifically to re-engage members who might not
    otherwise qualify; gating it behind the same eligibility rules as a
    normal redemption would defeat the purpose). No Transaction row is
    created (no purchase event occurred and no points were spent -- differs
    deliberately from redeem_reward(), which always writes both a
    Redemption and a Transaction). This is the one place this batch
    deviates from the established ledger validation path -- intentional,
    not a missed check (PLAN_BATCH3.md §4)."""
    redemption = Redemption(
        member_id=member.id,
        reward_id=reward.id,
        transaction_id=None,
        points_spent=0,
        status=RedemptionStatus.COMPLETED.value,
        source="winback",
    )
    db.add(redemption)
    db.flush()
    return redemption


def _already_offered_member_ids(db: Session, merchant_id: str) -> set[str]:
    rows = db.query(WinbackOffer.member_id).filter(WinbackOffer.merchant_id == merchant_id).all()
    return {row[0] for row in rows}


def _grant_and_record(
    db: Session,
    merchant: Merchant,
    rule: WinbackRule,
    reward: RewardCatalogItem,
    member: Member,
    churn_risk_score: float,
    triggered_by: str,
) -> WinbackOffer | None:
    """Grants the reward and records the WinbackOffer inside a SAVEPOINT
    (`db.begin_nested()`), not the outer transaction -- so that if the
    WinbackOffer.member_id unique constraint rejects this (a genuine
    concurrent-request race; the eligibility queries in the callers below
    are the primary guard and normally prevent this entirely), only this
    member's attempt is rolled back. Rolling back the *whole* session here
    would also discard whatever the caller already did earlier in the same
    request/transaction (e.g. §3's check_churn_escalations state updates
    when called from the auto-trigger path) -- a plain `db.rollback()`
    would be a correctness bug, not just an inefficiency.
    """
    try:
        with db.begin_nested():
            redemption = grant_winback_reward(db, member, reward)
            offer = WinbackOffer(
                merchant_id=merchant.id,
                member_id=member.id,
                rule_id=rule.id,
                redemption_id=redemption.id,
                churn_risk_score_at_trigger=churn_risk_score,
                triggered_by=triggered_by,
            )
            db.add(offer)
            db.flush()
    except IntegrityError:
        logger.info(
            "Win-back offer skipped (already offered): merchant=%s member=%s", merchant.id, member.id
        )
        return None
    return offer


def run_manual_winback(db: Session, merchant: Merchant) -> tuple[int, list[str]]:
    """`POST /api/v1/winback/run` -- PLAN_BATCH3.md §4 trigger path 1.

    Computes churn scores for all members (score_all_members, reused as-is,
    no AI-module changes) and, for every member whose score
    >= rule.churn_risk_threshold *and* who has no existing WinbackOffer
    *and* rule.enabled, grants the reward and records a
    WinbackOffer(triggered_by="manual"). Returns (offers_sent, member_ids).

    `rule.enabled=false` (or no rule saved yet) -> (0, []) regardless of
    eligible members -- an explicit off-switch."""
    rule = db.query(WinbackRule).filter(WinbackRule.merchant_id == merchant.id).first()
    if rule is None or not rule.enabled:
        return 0, []

    reward = db.get(RewardCatalogItem, rule.reward_id)
    if reward is None:
        return 0, []

    already_offered = _already_offered_member_ids(db, merchant.id)
    results = score_all_members(db, merchant.id)

    sent_member_ids: list[str] = []
    for result in results:
        if result.member_id in already_offered:
            continue
        if result.churn_risk_score < rule.churn_risk_threshold:
            continue
        member = db.get(Member, result.member_id)
        if member is None:
            continue
        offer = _grant_and_record(db, merchant, rule, reward, member, result.churn_risk_score, "manual")
        if offer is not None:
            sent_member_ids.append(member.id)
            already_offered.add(member.id)

    db.commit()
    return len(sent_member_ids), sent_member_ids


def maybe_auto_trigger_winback(
    db: Session,
    merchant: Merchant,
    escalated_members: list[Member],
    score_by_member_id: dict[str, float],
) -> list[str]:
    """PLAN_BATCH3.md §4 trigger path 2 -- automatic, piggybacking on §3's
    escalation detection. Called from
    app/api/ai.py::get_churn_scores *after* it calls
    app/services/notifications.py::check_churn_escalations, whose return
    value (`escalated_members`) is exactly "members that just transitioned
    into high risk this request" -- the signal this path depends on and
    does not recompute itself.

    For each escalated member, if `rule.enabled and rule.auto_trigger` and
    the member's churn score meets `rule.churn_risk_threshold`, runs the
    same grant-and-record flow, tagged triggered_by="auto".
    `auto_trigger` defaults to False on WinbackRule -- a deliberate,
    safety-first default (see WinbackRule's docstring) -- so this is a
    silent no-op for every merchant until they explicitly opt in, even if
    escalations are happening. Does NOT commit -- the caller (get_churn_scores)
    commits once for the whole request, alongside check_churn_escalations's
    own uncommitted state changes.
    """
    rule = db.query(WinbackRule).filter(WinbackRule.merchant_id == merchant.id).first()
    if rule is None or not rule.enabled or not rule.auto_trigger:
        return []

    reward = db.get(RewardCatalogItem, rule.reward_id)
    if reward is None:
        return []

    already_offered = _already_offered_member_ids(db, merchant.id)
    sent_member_ids: list[str] = []
    for member in escalated_members:
        if member.id in already_offered:
            continue
        score = score_by_member_id.get(member.id)
        if score is None or score < rule.churn_risk_threshold:
            continue
        offer = _grant_and_record(db, merchant, rule, reward, member, score, "auto")
        if offer is not None:
            sent_member_ids.append(member.id)
            already_offered.add(member.id)

    return sent_member_ids
