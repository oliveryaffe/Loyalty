"""GET /benchmark -- cross-merchant vertical benchmarking. Read-only,
gated the same as every other insight-presentation endpoint
(require_active_subscription, no admin requirement -- this is
presentation of a comparison, not a mutating action)."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import require_active_subscription
from app.db.base import get_db
from app.db.models import Merchant
from app.schemas.benchmark import BenchmarkOut
from app.services.benchmarking import compute_benchmark

router = APIRouter(prefix="/api/v1/benchmark", tags=["benchmark"])


@router.get("/repeat-visit-rate", response_model=BenchmarkOut)
def get_benchmark(
    db: Session = Depends(get_db),
    merchant: Merchant = Depends(require_active_subscription),
) -> BenchmarkOut:
    result = compute_benchmark(db, merchant)
    return BenchmarkOut(
        available=result.available,
        business_type=result.business_type,
        peer_count=result.peer_count,
        your_repeat_visit_rate=result.your_repeat_visit_rate,
        top_percent=result.top_percent,
        message=result.message,
    )
