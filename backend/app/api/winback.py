"""Win-back campaign rule + manual trigger + offer history (PLAN_BATCH3.md
§4). One rule per merchant (MVP scope, not a campaign builder).

Gated with `require_active_subscription`/`require_admin_active_subscription`
(not the older `get_current_merchant`), consistent with every other
paid-tier feature router in this batch -- win-back is a Growth-tier-and-up
feature per the pricing table, with no exemption reason like billing/auth/
webhooks/GDPR have.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import require_active_subscription, require_admin_active_subscription
from app.db.base import get_db
from app.db.models import Merchant, RewardCatalogItem, WinbackOffer, WinbackRule
from app.schemas.winback import WinbackOfferOut, WinbackRuleIn, WinbackRuleOut, WinbackRunResult
from app.services.winback import run_manual_winback

router = APIRouter(prefix="/api/v1/winback", tags=["winback"])


@router.get("/rule", response_model=WinbackRuleOut)
def get_rule(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> WinbackRuleOut:
    """Current rule, or a default-disabled shape if none saved yet."""
    rule = db.query(WinbackRule).filter(WinbackRule.merchant_id == merchant.id).first()
    if rule is None:
        return WinbackRuleOut(
            id=None,
            merchant_id=merchant.id,
            enabled=False,
            churn_risk_threshold=65.0,
            reward_id=None,
            auto_trigger=False,
            created_at=None,
            updated_at=None,
        )
    return rule


@router.put("/rule", response_model=WinbackRuleOut)
def upsert_rule(
    payload: WinbackRuleIn,
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> WinbackRuleOut:
    """Upsert the merchant's single rule. 400 if reward_id doesn't belong to
    this merchant or isn't active."""
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
            auto_trigger=payload.auto_trigger,
        )
        db.add(rule)
    else:
        rule.enabled = payload.enabled
        rule.churn_risk_threshold = payload.churn_risk_threshold
        rule.reward_id = payload.reward_id
        rule.auto_trigger = payload.auto_trigger

    db.commit()
    db.refresh(rule)
    return rule


@router.post("/run", response_model=WinbackRunResult)
def run_winback(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_admin_active_subscription),
) -> WinbackRunResult:
    """Manual trigger (PLAN_BATCH3.md §4 trigger path 1)."""
    offers_sent, member_ids = run_manual_winback(db, merchant)
    return WinbackRunResult(offers_sent=offers_sent, member_ids=member_ids)


@router.get("/offers", response_model=list[WinbackOfferOut])
def list_offers(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> list[WinbackOffer]:
    """History/audit list of WinbackOffer rows (who, when, which rule,
    manual vs auto) -- also doubles as the "did this already happen" view
    for the merchant."""
    return (
        db.query(WinbackOffer)
        .filter(WinbackOffer.merchant_id == merchant.id)
        .order_by(WinbackOffer.created_at.desc())
        .all()
    )
