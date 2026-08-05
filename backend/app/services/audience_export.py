"""Actionable audience exports (competitive-brief backlog item #4): the
at-risk member list should be one click away from a file a merchant can
import into whatever tool they already message customers with, rather
than Ledgerly becoming a campaign/messaging platform itself (a scope
creep this product has already deliberately backed away from once, with
win-back -- see app/services/winback.py).

Mailchimp and Klaviyo both accept a plain CSV list import keyed on email;
this module produces the same underlying rows (email, first name, last
name, a risk-band tag) and just varies the header row to match each
platform's expected column names so the file can be dragged straight into
their importer with no remapping. A true WhatsApp Business broadcast-list
export would need phone numbers, which Member does not currently collect
(see app/db/models.py) -- flagged as a known gap rather than silently
included with fake/missing data.

Erased members (GDPR right-to-erasure, app/api/members.py) are always
excluded -- their stored email is an anonymised `deleted.ledgerly.invalid`
placeholder, and re-exporting it anywhere would be both pointless and a
compliance smell for a product that markets GDPR-native handling.

Also supports exporting by next-best-product category (rather than risk
band) -- "everyone whose top suggestion is X" -- so a merchant running an
email or loyalty programme can plug a genuinely product-level segment
straight into a campaign, not just a risk-based list.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.ai.churn_model import compute_merchant_calibration, score_all_members
from app.ai.next_best_product import build_affinity_matrix, recommend_next_best
from app.db.models import Member

VALID_RISK_BANDS = ("high", "medium", "low")

# Column headers per destination -- same underlying data (email, first
# name, last name, a risk-band tag), just matching each platform's
# expected import format so no remapping step is needed on the way in.
AUDIENCE_EXPORT_FORMATS: dict[str, list[str]] = {
    "mailchimp": ["Email Address", "First Name", "Last Name", "Tags"],
    "klaviyo": ["Email", "First Name", "Last Name", "Tags"],
    "generic": ["email", "first_name", "last_name", "tags"],
}
DEFAULT_EXPORT_FORMAT = "generic"


def build_audience_export_rows(
    db: Session, merchant_id: str, risk_band: str | None = None
) -> list[tuple[str, str, str, str]]:
    """Returns (email, first_name, last_name, tag) rows, optionally
    filtered to a single risk band. Excludes members with no email (should
    not happen in practice, but a defensive skip is cheaper than a broken
    export row) and erased members (see module docstring)."""
    calibration = compute_merchant_calibration(db, merchant_id)
    results = score_all_members(db, merchant_id, calibration=calibration)
    if risk_band is not None:
        results = [r for r in results if r.risk_band == risk_band]
    if not results:
        return []

    member_ids = [r.member_id for r in results]
    members_by_id = {
        m.id: m
        for m in db.query(Member).filter(Member.id.in_(member_ids), Member.erased_at.is_(None)).all()
    }

    rows: list[tuple[str, str, str, str]] = []
    for r in results:
        member = members_by_id.get(r.member_id)
        if member is None or not member.email:
            continue
        tag = f"ledgerly-{r.risk_band}-risk"
        rows.append((member.email, member.first_name, member.last_name, tag))
    return rows


def build_next_best_export_rows(
    db: Session, merchant_id: str, category: str
) -> list[tuple[str, str, str, str]]:
    """Returns (email, first_name, last_name, tag) rows for every member
    whose #1 next-best-product suggestion (app/ai/next_best_product.py)
    matches `category`. Builds the affinity matrix once, then scores each
    member -- same "compute once per request" shape as
    build_audience_export_rows above and GET /insights/report.csv.
    Excludes members with no email or who've been GDPR-erased, same as
    the risk-based export."""
    matrix, granularity = build_affinity_matrix(db, merchant_id)
    if matrix.empty:
        return []

    members = (
        db.query(Member)
        .filter(Member.merchant_id == merchant_id, Member.erased_at.is_(None))
        .all()
    )

    rows: list[tuple[str, str, str, str]] = []
    for member in members:
        if not member.email:
            continue
        ranked = recommend_next_best(db, member, matrix, granularity, top_n=1)
        if not ranked or ranked[0].category != category:
            continue
        tag = f"ledgerly-next-best-{category}"
        rows.append((member.email, member.first_name, member.last_name, tag))
    return rows
