"""Shopify-style webhook ingestion (sandboxed demo -- no real Shopify store).

Deliberately NOT behind get_current_merchant/JWT: real Shopify webhooks
carry no user session, just an HMAC signature over the raw request body.
This endpoint reads the raw body via `await request.body()` *before* any
Pydantic parsing -- required because Shopify signs the raw bytes, and
re-serialized JSON does not byte-for-byte match what was signed. This is a
deliberate, webhook-specific deviation from the rest of the codebase's
typed-body-param style; do not "simplify" it back to a typed body param.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import Merchant
from app.schemas.shopify import ShopifyOrderWebhook
from app.schemas.transaction import TransactionOut
from app.services.shopify import ingest_shopify_order, verify_shopify_hmac

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

HMAC_HEADER = "X-Shopify-Hmac-Sha256"


@router.post("/shopify/{merchant_id}/orders-create")
async def shopify_orders_create(
    merchant_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    merchant = db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found")

    raw_body = await request.body()
    signature = request.headers.get(HMAC_HEADER)

    if not verify_shopify_hmac(raw_body, signature, merchant.shopify_webhook_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    try:
        raw_json = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Malformed JSON body") from exc

    try:
        payload = ShopifyOrderWebhook.model_validate(raw_json)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    txn, is_duplicate = ingest_shopify_order(db, merchant, payload)
    db.commit()

    if is_duplicate:
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "duplicate_ignored"})

    db.refresh(txn)
    body = json.loads(TransactionOut.model_validate(txn).model_dump_json())
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=body)
