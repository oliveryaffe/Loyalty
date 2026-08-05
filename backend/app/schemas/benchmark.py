"""Cross-merchant benchmarking schema -- see
app/services/benchmarking.py for the underlying computation."""
from pydantic import BaseModel


class BenchmarkOut(BaseModel):
    available: bool
    business_type: str | None
    peer_count: int
    your_repeat_visit_rate: float | None
    top_percent: int | None
    message: str
