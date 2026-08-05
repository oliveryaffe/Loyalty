"""Insights API: CSV upload ingestion, per-member + merchant-wide
future-value and next-best-product, and a combined CSV export
(PLAN_BATCH2.md §5). All endpoints are JWT-protected via
`require_active_subscription` (PLAN_BATCH3.md §2 -- hard-locks a lapsed
merchant, same as `ai.py`/`rewards.py`/`transactions.py`/most of
`members.py`) -- dashboard-initiated actions, same as `ai.py`
(unlike the Shopify webhook, which is a third-party callback)."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.ai.future_value import (
    predict_future_value,
    score_all_members_future_value,
    train_future_value_model,
)
from app.ai.next_best_product import build_affinity_matrix, recommend_next_best
from app.api.deps import require_active_subscription
from app.db.base import get_db
from app.db.models import Member, Merchant
from app.schemas.insights import FutureValueOut, InsightsUploadResult, NextBestOut
from app.services.csv_ingest import CsvUploadError, parse_and_ingest_csv
from app.services.usage import record_usage_event

router = APIRouter(prefix="/api/v1/insights", tags=["insights"])

DEFAULT_HORIZON_DAYS = 90
DEFAULT_TOP_N = 3


def _get_member_or_404(db: Session, member_id: str, merchant: Merchant) -> Member:
    member = (
        db.query(Member).filter(Member.id == member_id, Member.merchant_id == merchant.id).first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return member


def _to_future_value_out(r) -> FutureValueOut:
    return FutureValueOut(
        member_id=r.member_id,
        first_name=r.first_name,
        last_name=r.last_name,
        horizon_days=r.horizon_days,
        predicted_future_value=r.predicted_value,
        model_used=r.model_used,
        avg_order_value=r.avg_order_value,
        monthly_purchase_rate=r.monthly_purchase_rate,
    )


@router.post("/upload", response_model=InsightsUploadResult)
async def upload_insights_csv(
    file: UploadFile = File(...),
    mint_points: bool = Query(
        False,
        description=(
            "If true, also credits real loyalty points for each ingested row via the normal "
            "earn_points() ledger path. Default false: uploaded rows are treated as historical "
            "backfill data, not new purchases, so Member.points_balance is left untouched."
        ),
    ),
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> InsightsUploadResult:
    filename = (file.filename or "").lower()
    content_type = (file.content_type or "").lower()
    if not (filename.endswith(".csv") or "csv" in content_type):
        raise HTTPException(status_code=422, detail="File must be a .csv / text/csv file.")

    raw_bytes = await file.read()
    try:
        result = parse_and_ingest_csv(db, merchant, raw_bytes, mint_points=mint_points)
    except CsvUploadError as exc:
        # File-level failures are raised before any row is added to the
        # session, so there is nothing to roll back here -- see
        # csv_ingest.py's docstring.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Billable insight run (app/services/usage.py) -- recorded even if
    # every row failed validation; the merchant still asked Ledgerly to
    # process a file, which is the unit of work being priced, not row
    # count.
    record_usage_event(db, merchant, "csv_upload")
    db.commit()
    return result


@router.get("/future-value", response_model=list[FutureValueOut])
def get_future_value(
    horizon_days: int = Query(DEFAULT_HORIZON_DAYS, gt=0),
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> list[FutureValueOut]:
    results = score_all_members_future_value(db, merchant.id, horizon_days=horizon_days)
    return [_to_future_value_out(r) for r in results]


@router.get("/future-value/{member_id}", response_model=FutureValueOut)
def get_future_value_for_member(
    member_id: str,
    horizon_days: int = Query(DEFAULT_HORIZON_DAYS, gt=0),
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> FutureValueOut:
    member = _get_member_or_404(db, member_id, merchant)
    model = train_future_value_model(db, merchant.id)
    result = predict_future_value(db, member, model, horizon_days=horizon_days)
    return _to_future_value_out(result)


@router.get("/next-best-product/{member_id}", response_model=list[NextBestOut])
def get_next_best_product(
    member_id: str,
    top_n: int = Query(DEFAULT_TOP_N, gt=0),
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> list[NextBestOut]:
    member = _get_member_or_404(db, member_id, merchant)
    affinity_matrix, granularity = build_affinity_matrix(db, merchant.id)
    ranked = recommend_next_best(db, member, affinity_matrix, granularity, top_n=top_n)
    return [
        NextBestOut(
            category=r.category,
            product_name=r.product_name,
            score=r.score,
            reason=r.reason,
            data_granularity=granularity,
        )
        for r in ranked
    ]


@router.get("/report.csv")
def get_insights_report_csv(
    horizon_days: int = Query(DEFAULT_HORIZON_DAYS, gt=0),
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> StreamingResponse:
    """Combined future-value + next-best-product export, one row per
    member (PLAN_BATCH2.md §5). stdlib csv.writer into an io.StringIO --
    no new dependency."""
    members = db.query(Member).filter(Member.merchant_id == merchant.id).all()
    fv_model = train_future_value_model(db, merchant.id)
    affinity_matrix, granularity = build_affinity_matrix(db, merchant.id)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "member_id",
            "first_name",
            "last_name",
            "email",
            "tier",
            "predicted_future_value",
            "horizon_days",
            "model_used",
            "next_best_category",
            "next_best_product",
            "next_best_score",
        ]
    )
    for member in members:
        fv = predict_future_value(db, member, fv_model, horizon_days=horizon_days)
        nb = recommend_next_best(db, member, affinity_matrix, granularity, top_n=1)
        top = nb[0] if nb else None
        writer.writerow(
            [
                member.id,
                member.first_name,
                member.last_name,
                member.email,
                member.tier,
                fv.predicted_value,
                fv.horizon_days,
                fv.model_used,
                top.category if top else "",
                (top.product_name or "") if top else "",
                top.score if top else "",
            ]
        )

    buffer.seek(0)

    # Billable insight run (app/services/usage.py) -- a report export is
    # the other deliberate "turn my data into insight" action, alongside
    # CSV upload above.
    record_usage_event(db, merchant, "report_download")
    db.commit()

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="future_value_report.csv"'},
    )
