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
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.ai.churn_model import score_all_members
from app.db.models import Merchant, RewardCatalogItem, WinbackRule

DEFAULT_THRESHOLD = 65.0
MAX_WORKLIST_SIZE = 100


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
