"""Win-back worklist: a reward preference (threshold + which reward to
suggest) plus a computed, read-only list of at-risk members. Reworked
(see app/services/winback.py) from an auto-executing campaign feature --
there is no more "run", no more offer history, no more auto-trigger.

Gated with `require_active_subscription`/`require_admin_active_subscription`,
consistent with every other paid-tier feature router in this codebase.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import require_active_subscription, require_admin_active_subscription
from app.db.base import get_db
from app.db.models import Merchant, RewardCatalogItem, WinbackRule
from app.schemas.winback import WinbackRuleIn, WinbackRuleOut, WinbackWorklistEntryOut
from app.services.winback import get_winback_worklist

router = APIRouter(prefix="/api/v1/winback", tags=["winback"])


@router.get("/rule", response_model=WinbackRuleOut)
def get_rule(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> WinbackRuleOut:
    """Current reward preference, or a default-disabled shape if none saved yet."""
    rule = db.query(WinbackRule).filter(WinbackRule.merchant_id == merchant.id).first()
    if rule is None:
        return WinbackRuleOut(
            id=None,
            merchant_id=merchant.id,
            enabled=False,
            churn_risk_threshold=65.0,
            reward_id=None,
        )
    return rule


@router.put("/rule", response_model=WinbackRuleOut)
def upsert_rule(
    payload: WinbackRuleIn,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> WinbackRuleOut:
    """Upsert the merchant's single reward preference. 400 if reward_id
    doesn't belong to this merchant or isn't active."""
    reward = (
        db.query(RewardCatalogItem)
        .filter(RewardCatalogItem.id == payload.reward_id, RewardCatalogItem.merchant_id == merchant.id)
        .first()
    )
    if reward is None or not reward.active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reward_id must belong to this merchant and be active",
        )

    rule = db.query(WinbackRule).filter(WinbackRule.merchant_id == merchant.id).first()
    if rule is None:
        rule = WinbackRule(
            merchant_id=merchant.id,
            enabled=payload.enabled,
            churn_risk_threshold=payload.churn_risk_threshold,
            reward_id=payload.reward_id,
        )
        db.add(rule)
    else:
        rule.enabled = payload.enabled
        rule.churn_risk_threshold = payload.churn_risk_threshold
        rule.reward_id = payload.reward_id

    db.commit()
    db.refresh(rule)
    return rule


@router.get("/worklist", response_model=list[WinbackWorklistEntryOut])
def get_worklist(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> list[WinbackWorklistEntryOut]:
    """Who's at risk right now, and what to consider offering them.
    Computed live on every call -- no persistence, no "already sent"
    state, no execution. The merchant acts on this in whatever tool they
    already use to comp a reward; Ledgerly never grants anything itself."""
    entries = get_winback_worklist(db, merchant)
    return [
        WinbackWorklistEntryOut(
            member_id=e.member_id,
            first_name=e.first_name,
            last_name=e.last_name,
            churn_risk_score=e.churn_risk_score,
            risk_band=e.risk_band,
            suggested_reward_id=e.suggested_reward_id,
            suggested_reward_name=e.suggested_reward_name,
        )
        for e in entries
    ]
