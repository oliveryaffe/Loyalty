"""Schemas for the Batch 2 `insights` API surface: CSV upload results,
future-value predictions, next-best-product recommendations
(PLAN_BATCH2.md §5). Shapes copied 1:1 from the plan's schema block.
"""
from typing import Literal

from pydantic import BaseModel


class InsightsUploadRowError(BaseModel):
    row: int
    reason: str


class InsightsUploadResult(BaseModel):
    rows_received: int
    rows_ingested: int
    rows_skipped_duplicate: int
    rows_failed: int
    members_created: int
    errors: list[InsightsUploadRowError]


class FutureValueOut(BaseModel):
    member_id: str
    first_name: str
    last_name: str
    horizon_days: int
    predicted_future_value: float
    model_used: Literal["trained", "heuristic"]
    avg_order_value: float
    monthly_purchase_rate: float


class NextBestOut(BaseModel):
    category: str
    product_name: str | None
    score: float
    reason: str
    data_granularity: Literal["product", "category"]
