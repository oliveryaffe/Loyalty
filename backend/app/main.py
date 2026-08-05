"""FastAPI app entrypoint.

Run locally with:
    uvicorn app.main:app --reload

OpenAPI/Swagger docs auto-generated at /docs (see PLAN.md P3 acceptance item).
"""
from fastapi import FastAPI
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
