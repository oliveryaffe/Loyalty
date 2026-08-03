"""Next-best-product / next-best-category recommendation (PLAN_BATCH2.md
§4).

Item-based collaborative filtering over category (or product, if uploaded
product-level data is available). Standard "similarity-weighted sum of
what they already like" CF, not a trained model -- structured as a pure
scoring function so a real learned-ranking model could be swapped in
behind the same interface later, matching this codebase's existing
recommender.py/churn_model.py convention.

Data source, in priority order (see `build_affinity_matrix`):
1. If any `Transaction.product_category` is non-null for this merchant
   (CSV data has been uploaded): a genuine member x category purchase
   matrix, `data_granularity="product"` (product *names* are surfaced as a
   secondary "representative example" label within the winning category,
   not used as the CF substrate itself -- individual product names are too
   sparse for meaningful co-occurrence at demo scale).
2. Else (out-of-the-box seeded data, no upload yet): degrade to
   `Redemption` x `RewardCatalogItem.category` -- the same category-
   affinity signal recommender.py already reads for one member's own
   history, here aggregated across *all* members into a full item-based CF
   matrix. `data_granularity="category"`, `product_name` is always null.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Member, Redemption, RewardCatalogItem, Transaction

# A member's own affinity toward a category counts as "already engaged"
# above this normalized-spend-share threshold; categories at/below it are
# still eligible to be recommended (this is the "or engaged with below a
# low threshold" clause in the plan).
LOW_ENGAGEMENT_THRESHOLD = 0.05

Granularity = Literal["product", "category"]


@dataclass
class NextBestResult:
    category: str
    product_name: str | None
    score: float
    reason: str


def build_affinity_matrix(db: Session, merchant_id: str) -> tuple[pd.DataFrame, Granularity]:
    """member_id x category matrix, M[i][j] = total $ (or points, for the
    redemption fallback) member i has engaged category j with. Returns
    (matrix, granularity) -- an empty DataFrame if this merchant has
    neither uploaded product data nor any completed redemptions yet."""
    product_rows = (
        db.query(Transaction.member_id, Transaction.product_category, Transaction.amount_usd)
        .join(Member, Transaction.member_id == Member.id)
        .filter(Member.merchant_id == merchant_id, Transaction.product_category.isnot(None))
        .all()
    )
    if product_rows:
        df = pd.DataFrame(product_rows, columns=["member_id", "category", "amount_usd"])
        matrix = df.pivot_table(
            index="member_id", columns="category", values="amount_usd", aggfunc="sum", fill_value=0.0
        )
        return matrix, "product"

    redemption_rows = (
        db.query(Redemption.member_id, RewardCatalogItem.category, Redemption.points_spent)
        .join(RewardCatalogItem, Redemption.reward_id == RewardCatalogItem.id)
        .filter(RewardCatalogItem.merchant_id == merchant_id, Redemption.status == "completed")
        .all()
    )
    if redemption_rows:
        df = pd.DataFrame(redemption_rows, columns=["member_id", "category", "points_spent"])
        matrix = df.pivot_table(
            index="member_id", columns="category", values="points_spent", aggfunc="sum", fill_value=0.0
        )
        return matrix, "category"

    return pd.DataFrame(), "category"


def _representative_product(db: Session, merchant_id: str, category: str) -> str | None:
    """Within `category`, the single most-purchased-by-similar-members
    product (by total $ across this merchant's uploaded transactions) --
    only meaningful when granularity="product"."""
    row = (
        db.query(Transaction.product_name, func.sum(Transaction.amount_usd).label("total"))
        .join(Member, Transaction.member_id == Member.id)
        .filter(
            Member.merchant_id == merchant_id,
            Transaction.product_category == category,
            Transaction.product_name.isnot(None),
        )
        .group_by(Transaction.product_name)
        .order_by(func.sum(Transaction.amount_usd).desc())
        .first()
    )
    return row[0] if row else None


def _popularity_fallback(
    db: Session, merchant_id: str, affinity_matrix: pd.DataFrame, granularity: Granularity, top_n: int
) -> list[NextBestResult]:
    """Cold-start fallback (plan step 5): a member with no purchase/
    redemption history at all -- rank by global category popularity
    (highest total spend/redemption across all members) instead of
    similarity-weighted CF, which needs the member's own row to work from."""
    if affinity_matrix.empty:
        return []
    popularity = affinity_matrix.sum(axis=0).sort_values(ascending=False)
    results = []
    for category, total in popularity.head(top_n).items():
        product_name = _representative_product(db, merchant_id, category) if granularity == "product" else None
        results.append(
            NextBestResult(
                category=str(category),
                product_name=product_name,
                score=round(float(total), 4),
                reason="popular across all members (cold-start fallback -- no purchase/redemption history yet)",
            )
        )
    return results


def recommend_next_best(
    db: Session,
    member: Member,
    affinity_matrix: pd.DataFrame,
    granularity: Granularity,
    top_n: int = 3,
) -> list[NextBestResult]:
    """Item-based CF: rank categories the member hasn't already substantially
    engaged with by how similar they are (cosine similarity over the
    member x category matrix, category-as-vector) to categories the member
    *does* already engage with."""
    if affinity_matrix.empty or member.id not in affinity_matrix.index:
        return _popularity_fallback(db, member.merchant_id, affinity_matrix, granularity, top_n)

    member_row = affinity_matrix.loc[member.id]
    total = float(member_row.sum())
    if total <= 0:
        return _popularity_fallback(db, member.merchant_id, affinity_matrix, granularity, top_n)

    categories = list(affinity_matrix.columns)
    v = (member_row / total).reindex(categories).fillna(0.0)

    # Category-category cosine similarity, treating each category as a
    # vector of member-affinities (standard item-based CF substrate).
    sim = cosine_similarity(affinity_matrix.T.values)
    sim_df = pd.DataFrame(sim, index=categories, columns=categories)

    # score(c) = sum_{j != c} v[j] * S[j][c]  -- vectorized as (v @ S) with
    # the j=c self-similarity term (v[c] * S[c][c], and S[c][c] == 1)
    # subtracted back out.
    raw_scores = v.values @ sim_df.values
    adjusted_scores = pd.Series(raw_scores - v.values, index=categories)

    eligible = [c for c in categories if v[c] <= LOW_ENGAGEMENT_THRESHOLD]
    if not eligible:
        # Member has meaningfully engaged with every known category --
        # still return a ranking rather than nothing.
        eligible = categories

    ranked = adjusted_scores.loc[eligible].sort_values(ascending=False).head(top_n)

    results = []
    for category, score in ranked.items():
        product_name = _representative_product(db, member.merchant_id, category) if granularity == "product" else None
        results.append(
            NextBestResult(
                category=str(category),
                product_name=product_name,
                score=round(float(score), 4),
                reason="similar to categories you already engage with (item-based collaborative filtering)",
            )
        )
    return results
