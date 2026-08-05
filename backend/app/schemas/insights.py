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


class SampleDataRequest(BaseModel):
    business_type: str


class SampleDataOut(BaseModel):
    """Result of app/services/sample_data.py::generate_sample_dataset --
    a vertical-specific starter dataset (members, transactions, reward
    catalog), only ever generated for an account with zero real data."""

    business_type: str
    members_created: int
    transactions_created: int
    rewards_created: int


class SampleDataStatusOut(BaseModel):
    """Powers the "you're viewing sample data" banner -- True whenever
    every member on the account right now is sample data (see
    app/services/sample_data.py::is_viewing_sample_data)."""

    is_sample_data: bool


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
