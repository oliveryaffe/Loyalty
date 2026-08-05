"""Schemas for the Batch 2 `insights` API surface: CSV upload results,
future-value predictions, next-best-product recommendations
(PLAN_BATCH2.md §5). Shapes copied 1:1 from the plan's schema block.
"""
from datetime import datetime
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


class RevenueAtRiskOut(BaseModel):
    total_future_value_gbp: float
    at_risk_future_value_gbp: float
    at_risk_share: float | None
    headline: str


class TrendOut(BaseModel):
    previous_captured_at: datetime
    days_since_previous: int
    high_risk_count_delta: int
    at_risk_future_value_gbp_delta: float
    headline: str


class ChurnDriverOut(BaseModel):
    dominant_driver: Literal["recency", "frequency", "monetary"] | None
    share_of_high_risk: float
    headline: str


class CategoryPerformanceOut(BaseModel):
    category: str
    source: Literal["redemption", "purchase"]
    engaged_members: int
    avg_future_value_gbp: float
    lift_pct: float


class BusinessInsightsOut(BaseModel):
    """Business-level "so what" report -- app/services/business_insights.py.
    Powers a new panel on the Insights page, above the per-member table:
    ties churn risk + future value into one revenue-at-risk headline, a
    trend vs the last time this was measured, the dominant reason behind
    the current at-risk cohort, and which reward/purchase categories are
    actually associated with higher-value customers."""

    generated_at: datetime
    total_members: int
    revenue_at_risk: RevenueAtRiskOut
    trend: TrendOut | None
    churn_driver: ChurnDriverOut | None
    top_categories: list[CategoryPerformanceOut]
