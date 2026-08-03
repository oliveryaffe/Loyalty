"""Upload a sample product-transaction CSV to a running Ledgerly instance --
the no-hand-authored-file-needed way to demo/verify the Batch 2 CSV upload
path end-to-end. Mirrors send_sample_shopify_webhook.py's CLI shape.

Usage (from backend/, against a locally running `uvicorn` instance seeded
via `python scripts/seed_data.py`):

    python scripts/send_sample_csv_upload.py \\
        --base-url http://127.0.0.1:8000 \\
        --merchant-email demo@merchant.com \\
        --merchant-password demo1234

Or point at a different CSV file with --fixture. Pass --mint-points to
also credit real loyalty points for the uploaded rows (off by default,
matching the upload endpoint's own default -- see PLAN_BATCH2.md §2).

Exit code 0 on success (prints the InsightsUploadResult summary);
non-zero with a clear message on any failure (bad login, non-2xx
response, etc.).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "sample_product_transactions.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a sample product-transaction CSV.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL of the running API.")
    parser.add_argument("--merchant-email", default="demo@merchant.com", help="Team member login email.")
    parser.add_argument("--merchant-password", default="demo1234", help="Team member login password.")
    parser.add_argument(
        "--fixture", default=str(FIXTURE_PATH), help="Path to the sample product-transactions CSV file."
    )
    parser.add_argument(
        "--mint-points",
        action="store_true",
        help="Also credit real loyalty points for the uploaded rows (default: backfill only, no points minted).",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")

    login_resp = httpx.post(
        f"{base_url}/api/v1/auth/login",
        json={"email": args.merchant_email, "password": args.merchant_password},
        timeout=10.0,
    )
    if login_resp.status_code != 200:
        print(f"error: login failed ({login_resp.status_code}): {login_resp.text}", file=sys.stderr)
        return 1
    token = login_resp.json()["access_token"]

    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        print(f"error: fixture file not found: {fixture_path}", file=sys.stderr)
        return 1

    url = f"{base_url}/api/v1/insights/upload?mint_points={'true' if args.mint_points else 'false'}"
    print(f"POST {url}")
    with fixture_path.open("rb") as fh:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (fixture_path.name, fh, "text/csv")},
            timeout=30.0,
        )

    if resp.status_code != 200:
        print(f"error: unexpected status {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1

    body = resp.json()
    print("Upload result:")
    print(f"  rows_received:          {body['rows_received']}")
    print(f"  rows_ingested:          {body['rows_ingested']}")
    print(f"  rows_skipped_duplicate: {body['rows_skipped_duplicate']}")
    print(f"  rows_failed:            {body['rows_failed']}")
    print(f"  members_created:        {body['members_created']}")
    if body["errors"]:
        print("  errors:")
        for err in body["errors"][:10]:
            print(f"    row {err['row']}: {err['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
