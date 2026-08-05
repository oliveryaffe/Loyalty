"""FastAPI app entrypoint.

Run locally with:
    uvicorn app.main:app --reload

OpenAPI/Swagger docs auto-generated at /docs (see PLAN.md P3 acceptance item).
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    ai,
    auth,
    benchmark,
    billing,
    digest,
    experiments,
    gdpr,
    insights,
    locations,
    members,
    rewards,
    settings as settings_api,
    team,
    transactions,
    webhooks,
    winback,
)
from app.config import settings
from app.db.base import init_db

app = FastAPI(
    title="Loyalty AI Framework API",
    description=(
        "B2B loyalty-platform MVP: points ledger, reward catalog/redemption, "
        "and an AI layer (reward recommendations, churn risk, fraud detection)."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/_internal/reseed-demo-data", tags=["health"], include_in_schema=False)
def reseed_demo_data(secret: str = Query(...)) -> dict[str, object]:
    """TEMPORARY one-off maintenance endpoint (see app/config.py's
    admin_reseed_secret docstring). Re-runs scripts/seed_data.py::seed(
    reset=True) against this process's own DATABASE_URL -- used once to
    repair the production demo@merchant.com account after the Next Best
    Product product-tagging fix, then this endpoint is deleted. 404s
    (rather than 403s) when unset/mismatched, so it doesn't advertise its
    own existence.
    """
    if not settings.admin_reseed_secret or secret != settings.admin_reseed_secret:
        raise HTTPException(status_code=404)

    from scripts.seed_data import seed

    seed(reset=True)
    from app.db.base import SessionLocal
    from app.db.models import Member, Merchant, Transaction

    db = SessionLocal()
    try:
        return {
            "status": "reseeded",
            "merchants": db.query(Merchant).count(),
            "members": db.query(Member).count(),
            "transactions": db.query(Transaction).count(),
        }
    finally:
        db.close()



app.include_router(auth.router)
app.include_router(members.router)
app.include_router(transactions.router)
app.include_router(rewards.router)
app.include_router(ai.router)
app.include_router(team.router)
app.include_router(webhooks.router)
app.include_router(insights.router)
app.include_router(billing.router)
app.include_router(settings_api.router)
app.include_router(winback.router)
app.include_router(experiments.router)
app.include_router(gdpr.router)
app.include_router(digest.router)
app.include_router(benchmark.router)
app.include_router(locations.router)
