"""CSV upload ingestion for historical/backfill product-transaction data
(PLAN_BATCH2.md §2).

Kept as a pure-ish service function (mirrors app/services/shopify.py's
`ingest_shopify_order` shape: takes a DB session + merchant + parsed input,
returns a result, no HTTP concerns) so it's easy to unit test without
spinning up the HTTP layer. Uses only the stdlib `csv` module -- no new
dependency needed.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone

from email_validator import EmailNotValidError, validate_email
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Member, Merchant, Transaction, TransactionType
from app.schemas.insights import InsightsUploadResult, InsightsUploadRowError
from app.services.ledger import earn_points

MAX_UPLOAD_ROWS = 20_000

REQUIRED_HEADERS = {"customer_email", "transaction_date", "amount_gbp"}
ALLOWED_CHANNELS = {"pos", "online", "mobile"}
MAX_CATEGORY_LEN = 60
MAX_PRODUCT_NAME_LEN = 150


class CsvUploadError(ValueError):
    """File-level validation failure (not row-level). Callers should map
    this to a 422/400 response with nothing ingested -- see PLAN_BATCH2.md
    §2 "File-level (reject whole upload, 422)"."""


@dataclass
class _ParsedRow:
    email: str
    transaction_date: datetime
    amount_gbp: float
    product_category: str | None
    product_name: str | None
    channel: str
    external_order_id: str | None
    first_name: str
    last_name: str


def _parse_date(raw: str) -> datetime:
    """Accepts YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS (ISO 8601). Raises
    ValueError on anything else -- caller turns that into a row-level
    error."""
    dt = datetime.fromisoformat(raw.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_and_ingest_csv(
    db: Session,
    merchant: Merchant,
    raw_csv_bytes: bytes,
    mint_points: bool = False,
) -> InsightsUploadResult:
    """Parse an uploaded product-transaction CSV and ingest valid rows as
    `Transaction` rows for `merchant`.

    `mint_points=False` (the default -- see PLAN_BATCH2.md §2 "Does
    uploading mint real loyalty points?"): rows become real
    `Transaction(type="earn", source="csv_upload", ...)` rows -- so they
    feed the future-value/next-best-product models and RFM -- but
    `Member.points_balance` is deliberately NOT incremented. Uploaded rows
    are historical backfill, not new live purchases; naively wiring the row
    loop straight into `earn_points()` would silently mint months of
    retroactive points against real balances, which is the single
    highest-risk mistake this feature could ship with. For the same reason,
    `Member.last_activity_at` is also left untouched on this path -- a
    backfilled six-month-old purchase should not make a member look
    "active today".

    `mint_points=True` opts into calling the existing
    `app.services.ledger.earn_points()` per row instead (real balance
    increase, same points-per-pound math as every other earn path, and it
    *does* advance `last_activity_at` via `occurred_at`) -- for the less
    common case of uploading genuinely new, not-yet-ledgered purchases.

    Raises `CsvUploadError` for file-level problems (empty file, missing
    required headers, too many rows) -- the caller (app/api/insights.py)
    maps this to a 422 with nothing ingested. Row-level problems (bad date,
    bad amount, bad email) are collected into the returned
    `InsightsUploadResult.errors` instead of raising -- one bad row never
    aborts the rest of the file.

    All valid rows are `db.add()`-ed but NOT committed here -- the caller
    commits once at the end (single `db.commit()`), so a crash mid-file
    can't leave a half-applied upload (PLAN_BATCH2.md §2 "Atomicity").
    """
    if not raw_csv_bytes:
        raise CsvUploadError("Uploaded file is empty.")

    try:
        text = raw_csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvUploadError("Uploaded file is not valid UTF-8 text/CSV.") from exc

    if not text.strip():
        raise CsvUploadError("Uploaded file is empty.")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CsvUploadError("Uploaded file has no header row.")

    # Case-insensitive, order-independent header lookup.
    header_map: dict[str, str] = {}
    for raw_header in reader.fieldnames:
        if raw_header is None:
            continue
        header_map[raw_header.strip().lower()] = raw_header

    missing = REQUIRED_HEADERS - set(header_map.keys())
    if missing:
        raise CsvUploadError(f"Missing required column(s): {', '.join(sorted(missing))}")

    rows = list(reader)
    if len(rows) > MAX_UPLOAD_ROWS:
        raise CsvUploadError(f"Too many rows ({len(rows)}); max is {MAX_UPLOAD_ROWS} per upload.")

    def _get(row: dict, key: str) -> str:
        header = header_map.get(key)
        if header is None:
            return ""
        val = row.get(header)
        return val.strip() if isinstance(val, str) else ""

    errors: list[InsightsUploadRowError] = []
    rows_ingested = 0
    rows_skipped_duplicate = 0
    rows_failed = 0
    members_created = 0

    # Cache of normalized-email -> Member for this batch, so (a) repeated
    # rows for the same new customer don't create duplicate Members, and
    # (b) we don't re-query the DB once per row for members we've already
    # resolved/created in this same upload.
    member_cache: dict[str, Member] = {}
    seen_external_order_ids: set[str] = set()

    for i, row in enumerate(rows):
        row_num = i + 2  # 1-based, +1 to account for the header row

        email_raw = _get(row, "customer_email")
        if not email_raw:
            errors.append(InsightsUploadRowError(row=row_num, reason="missing customer_email"))
            rows_failed += 1
            continue
        try:
            email = validate_email(email_raw, check_deliverability=False).normalized
        except EmailNotValidError as exc:
            errors.append(InsightsUploadRowError(row=row_num, reason=f"invalid customer_email: {exc}"))
            rows_failed += 1
            continue

        date_raw = _get(row, "transaction_date")
        if not date_raw:
            errors.append(InsightsUploadRowError(row=row_num, reason="missing transaction_date"))
            rows_failed += 1
            continue
        try:
            transaction_date = _parse_date(date_raw)
        except ValueError:
            errors.append(
                InsightsUploadRowError(row=row_num, reason=f"unparseable transaction_date: {date_raw!r}")
            )
            rows_failed += 1
            continue

        amount_raw = _get(row, "amount_gbp")
        if not amount_raw:
            errors.append(InsightsUploadRowError(row=row_num, reason="missing amount_gbp"))
            rows_failed += 1
            continue
        try:
            amount_gbp = float(amount_raw)
        except ValueError:
            errors.append(InsightsUploadRowError(row=row_num, reason=f"non-numeric amount_gbp: {amount_raw!r}"))
            rows_failed += 1
            continue
        if not (0 < amount_gbp <= settings.max_transaction_amount_gbp):
            errors.append(
                InsightsUploadRowError(
                    row=row_num,
                    reason=(
                        f"amount_gbp {amount_gbp} out of range "
                        f"(0, {settings.max_transaction_amount_gbp}]"
                    ),
                )
            )
            rows_failed += 1
            continue

        external_order_id = _get(row, "external_order_id") or None
        if external_order_id:
            if external_order_id in seen_external_order_ids:
                rows_skipped_duplicate += 1
                continue
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
                rows_skipped_duplicate += 1
                continue

        product_category = _get(row, "product_category")[:MAX_CATEGORY_LEN] or None
        product_name = _get(row, "product_name")[:MAX_PRODUCT_NAME_LEN] or None
        channel = _get(row, "channel").lower() or "pos"
        if channel not in ALLOWED_CHANNELS:
            channel = "pos"

        member = member_cache.get(email)
        if member is None:
            member = (
                db.query(Member)
                .filter(Member.merchant_id == merchant.id, Member.email == email)
                .first()
            )
        if member is None:
            member = Member(
                merchant_id=merchant.id,
                first_name=_get(row, "customer_first_name") or "Unknown",
                last_name=_get(row, "customer_last_name") or "Customer",
                email=email,
            )
            db.add(member)
            db.flush()
            members_created += 1
        member_cache[email] = member

        if mint_points:
            txn = earn_points(db, member, amount_gbp, channel=channel, occurred_at=transaction_date)
        else:
            # Historical backfill: create the ledger row directly (NOT via
            # earn_points()) so points_balance/last_activity_at are left
            # untouched -- see docstring above. `points` is left at 0
            # (rather than the would-be-earned amount) so nothing in the
            # ledger implies points were minted for this row.
            txn = Transaction(
                member_id=member.id,
                type=TransactionType.EARN.value,
                amount_gbp=amount_gbp,
                points=0,
                channel=channel,
                created_at=transaction_date,
            )
            db.add(txn)
            db.flush()

        txn.source = "csv_upload"
        txn.product_category = product_category
        txn.product_name = product_name
        if external_order_id:
            txn.external_order_id = external_order_id
            seen_external_order_ids.add(external_order_id)
        db.flush()

        rows_ingested += 1

    return InsightsUploadResult(
        rows_received=len(rows),
        rows_ingested=rows_ingested,
        rows_skipped_duplicate=rows_skipped_duplicate,
        rows_failed=rows_failed,
        members_created=members_created,
        errors=errors,
    )
