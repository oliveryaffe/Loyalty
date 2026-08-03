"""Send a realistic sample Shopify `orders/create` webhook to a running
Ledgerly instance, with a correctly computed HMAC signature -- the
no-real-Shopify-account-needed way to demo/verify Feature 2 end-to-end.

Usage (from backend/, against a locally running `uvicorn` instance seeded
via `python scripts/seed_data.py`):

    python scripts/send_sample_shopify_webhook.py \\
        --base-url http://127.0.0.1:8000 \\
        --merchant-email demo@merchant.com \\
        --secret demo-shopify-secret-change-me

Or pass --merchant-id directly if you already know it (skips the DB
lookup):

    python scripts/send_sample_shopify_webhook.py --merchant-id <id> --secret ...

Exit code 0 on success (prints the created/duplicate-ignored transaction
outcome and the member's new points balance); non-zero with a clear
message on any failure (bad signature, non-2xx response, etc.).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "shopify_order_create_sample.json"


def compute_hmac(raw_body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()).decode(
        "utf-8"
    )


def resolve_merchant_id(base_url: str, merchant_email: str) -> str:
    """Lightweight local DB read to find the merchant id for a given demo
    merchant email -- avoids requiring the caller to already know the id.
    Imports the app's own DB session, so this only works when run against
    the same DATABASE_URL the target server is using (true for the local
    dev / demo use case this script targets).
    """
    from app.db.base import SessionLocal
    from app.db.models import TeamMember

    db = SessionLocal()
    try:
        team_member = db.query(TeamMember).filter(TeamMember.email == merchant_email).first()
        if team_member is None:
            print(
                f"error: no team member found with email {merchant_email!r} in the local DB "
                "-- pass --merchant-id directly instead, or seed the DB first.",
                file=sys.stderr,
            )
            sys.exit(1)
        return team_member.merchant_id
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a sample Shopify orders/create webhook.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL of the running API.")
    parser.add_argument(
        "--merchant-email",
        default="demo@merchant.com",
        help="Email of a team member belonging to the target merchant (used to look up merchant_id).",
    )
    parser.add_argument("--merchant-id", default=None, help="Merchant id directly -- skips the DB lookup.")
    parser.add_argument(
        "--secret",
        default="demo-shopify-secret-change-me",
        help="Shared secret to sign the payload with (must match Merchant.shopify_webhook_secret).",
    )
    parser.add_argument(
        "--fixture",
        default=str(FIXTURE_PATH),
        help="Path to the sample Shopify order JSON fixture.",
    )
    args = parser.parse_args()

    merchant_id = args.merchant_id or resolve_merchant_id(args.base_url, args.merchant_email)

    raw_body = Path(args.fixture).read_bytes()
    signature = compute_hmac(raw_body, args.secret)

    url = f"{args.base_url.rstrip('/')}/api/v1/webhooks/shopify/{merchant_id}/orders-create"
    print(f"POST {url}")
    try:
        resp = httpx.post(
            url,
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Hmac-Sha256": signature,
                "X-Shopify-Topic": "orders/create",
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        print(f"error: request failed: {exc}", file=sys.stderr)
        return 1

    if resp.status_code not in (200, 201):
        print(f"error: unexpected status {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1

    body = resp.json()
    if resp.status_code == 200 and body.get("status") == "duplicate_ignored":
        print("Webhook accepted as a duplicate (idempotent replay) -- no new transaction created.")
        return 0

    print(f"Success: created transaction id={body.get('id')} points={body.get('points')}")

    fixture_payload = json.loads(raw_body)
    customer_email = fixture_payload["customer"]["email"]
    try:
        from app.db.base import SessionLocal
        from app.db.models import Member

        db = SessionLocal()
        try:
            member = (
                db.query(Member)
                .filter(Member.merchant_id == merchant_id, Member.email == customer_email)
                .first()
            )
            if member is not None:
                print(f"Member {member.email} new points_balance: {member.points_balance}")
        finally:
            db.close()
    except Exception:
        pass  # balance printout is best-effort; the webhook call itself already succeeded

    return 0


if __name__ == "__main__":
    sys.exit(main())
