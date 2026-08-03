"""Shopify webhook verification + order-to-transaction ingestion.

Kept as pure functions (mirrors app/services/ledger.py's style) so it's
easy to unit test without spinning up the HTTP layer. No ledger math is
duplicated here -- this module resolves/creates a Member from the webhook
payload and then delegates the actual points math to the existing
app.services.ledger.earn_points.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Member, Merchant, Transaction
from app.schemas.shopify import ShopifyOrderWebhook
from app.services.ledger import earn_points


def verify_shopify_hmac(raw_body: bytes, signature_header: str | None, secret: str | None) -> bool:
    """Recompute base64(HMAC-SHA256(raw_body, secret)) and compare to the
    `X-Shopify-Hmac-Sha256` header using a constant-time comparison.

    Returns False for a missing header or an unconfigured (empty/None)
    secret -- "no secret configured" must never be treated as "skip
    verification".
    """
    if not signature_header or not secret:
        return False

    computed = base64.b64encode(
        hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(computed, signature_header)


def ingest_shopify_order(
    db: Session, merchant: Merchant, payload: ShopifyOrderWebhook
) -> tuple[Transaction | None, bool]:
    """Ingest a validated Shopify order webhook. Returns
    (created_transaction_or_none, is_duplicate).

    Idempotency: if a Transaction already exists with this
    external_order_id for one of this merchant's members, this is a
    redelivery -- return (None, True) with no double-credit.

    Concurrency note: the SELECT below is a cheap fast-path check only --
    it is NOT what guarantees correctness. Two concurrent deliveries of the
    same webhook (a real payment processor's webhook system *will* retry
    on timeout/5xx) can both pass this SELECT before either has committed,
    which is exactly the TOCTOU race the original bug report found via
    forced barrier-synchronized interleaving (2 Transaction rows for the
    same external_order_id, plus a corrupted points_balance from the
    matching earn_points() lost-update). The actual source of truth is the
    DB-level UNIQUE constraint on Transaction.external_order_id (see
    app/db/models.py): whichever concurrent request's flush/commit reaches
    the database first wins, and every loser gets an IntegrityError caught
    below and is correctly treated as "already processed" instead of
    creating a duplicate row.
    """
    external_order_id = str(payload.id)

    existing = (
        db.query(Transaction)
        .join(Member, Transaction.member_id == Member.id)
        .filter(
            Member.merchant_id == merchant.id,
            Transaction.external_order_id == external_order_id,
        )
        .first()
    )
    if existing is not None:
        return None, True

    member = (
        db.query(Member)
        .filter(Member.merchant_id == merchant.id, Member.email == payload.customer.email)
        .first()
    )
    if member is None:
        member = Member(
            merchant_id=merchant.id,
            first_name=payload.customer.first_name or "Shopify",
            last_name=payload.customer.last_name or "Customer",
            email=payload.customer.email,
        )
        db.add(member)
        db.flush()

    amount_gbp = float(payload.total_price)
    txn = earn_points(db, member, amount_gbp, channel="shopify")
    txn.external_order_id = external_order_id
    txn.source = "shopify"

    try:
        db.flush()
    except IntegrityError:
        # Another concurrent request won the race and already committed a
        # Transaction with this external_order_id -- caught here via the
        # DB-level UNIQUE constraint, not the SELECT above. Roll back this
        # request's own work (including the points_balance increment
        # earn_points() just applied and any speculative Member it
        # created) and report it as a duplicate redelivery, same as the
        # fast-path case above.
        db.rollback()
        return None, True

    return txn, False
