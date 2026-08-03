# PLAN_BATCH2.md — Ledgerly, Feature Batch 2: Future Value & Next Best Product

**Scope:** one new feature end-to-end: per-member **predicted future value**
(forward-looking spend estimate) and **next-best-product/category**
recommendation, exposed via a new `insights` API surface, an optional richer
CSV-upload ingestion path, and a new **Insights** dashboard page. Builds on
top of the already-shipped MVP + Batch 1 (README.md, PLAN_BATCH1.md, 69
passing tests, `backend/app/db/models.py`, `backend/app/ai/*`). No other
behavior changes are in scope.

**Baseline being extended:** FastAPI + SQLAlchemy 2.0 (SQLite locally,
Postgres in prod), JWT auth via `TeamMember`/`get_current_merchant`
(`app/api/deps.py`), points ledger (`app/services/ledger.py`), three
in-process AI modules (`app/ai/recommender.py`, `churn_model.py`,
`fraud_detector.py` — all scikit-learn/pandas/numpy, no external services,
each documented as "pure scoring function, swappable later"), 620 seeded
members / ~7,200 transactions / an 18-item reward catalog
(`backend/scripts/seed_data.py`, Northwind Coffee demo merchant), 69
passing pytest tests.

---

## 1. Data model decision

**Decision: add two nullable columns directly to the existing `Transaction`
table — no new table.**

```python
# app/db/models.py, Transaction
product_category: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
product_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
```

### Why this over a new `Product`/`product_transactions` table

- **Precedent already exists in this exact codebase.** Batch 1 added
  `external_order_id`, `source`, and two `Merchant.shopify_*` columns the
  same way — nullable, defaulted, additive — specifically to avoid an
  Alembic migration and to keep `Base.metadata.create_all()`
  (non-destructive, only creates missing tables) sufficient. `Transaction`
  is already the single ledger row representing "one purchase event"; a
  product name/category is just two more attributes of that same event,
  not a new entity with its own lifecycle (no product catalog CRUD, no
  price history, no SKU management is in scope — the task is a report, not
  a PIM).
- **Single source of truth for every AI module that already reads
  `member.transactions`.** `churn_model.compute_rfm` and
  `fraud_detector._transactions_to_frame` both iterate
  `member.transactions` / query `Transaction` directly. If product data
  lived in a parallel table, every consumer (existing and new) would need
  a join, and the two tables could drift out of sync (a `Transaction` with
  no matching product row, or vice versa). Keeping it on `Transaction`
  means "richer data" is strictly *more columns filled in on the same
  rows", not a second ledger to reconcile.
- **Rejected alternative — a new `product_transactions` staging table:**
  would be cleaner in a pure line-item sense (one order can have multiple
  products) but is overkill here: the CSV schema below is one
  row-per-purchase-event, matching `Transaction`'s existing one-row-per-
  purchase-event shape. Multi-line-item orders are explicitly out of scope
  (documented in §2) — a merchant uploading a 3-item basket would upload 3
  rows, each becoming its own `Transaction`, exactly like today's
  single-item-implicit `POST /transactions` and the Shopify webhook (which
  also collapses `line_items` into one earn transaction against
  `total_price`, per `app/services/shopify.py`).
- **Backward compatibility is structurally guaranteed, not just claimed:**
  both new columns are `nullable=True` with no default-value side effects,
  so every one of the ~7,200 existing seeded `Transaction` rows (and every
  row any test creates without these fields) is valid as-is.

### Confirmed: does not break the 69 existing tests

Grepped `backend/tests/` for every direct `Transaction(...)` construction:
**exactly one call site**, `tests/test_fraud_detector.py:15` (`_mk_txn`
helper), and it uses keyword arguments only (`id=`, `member_id=`, `type=`,
`amount_usd=`, `points=`, `created_at=`) — no positional args, so adding
two more nullable columns cannot break it. Every other test that touches
`Transaction` goes through `POST /api/v1/transactions` or the seed script,
both of which also use kwargs/schema-based construction. **No Alembic
migration needed** — same clean-cutover / `create_all()` reasoning
PLAN_BATCH1.md §"Migration strategy" already established for this app
(pre-launch demo, no real customer data to preserve). Net: additive,
nullable, zero-risk to the existing suite — same risk shape as Batch 1's
`external_order_id`/`source` addition, which shipped with zero regressions.

---

## 2. CSV upload mechanism

### New endpoint

`POST /api/v1/insights/upload` (JWT-protected, `get_current_merchant` —
unlike the Shopify webhook, this is a merchant-initiated dashboard action,
not a third-party callback) — `multipart/form-data`, field name `file`.
Query param `mint_points: bool = false` (see below).

### Exact CSV schema

Header row required, columns (case-insensitive, order-independent):

| Column | Required | Type | Notes |
|---|---|---|---|
| `customer_email` | yes | string | Matched against `Member.email` for this merchant; auto-creates a new `Member` if not found (same pattern as `ingest_shopify_order`) |
| `customer_first_name` | no | string | Only used if a new `Member` is being created; defaults to `"Unknown"` |
| `customer_last_name` | no | string | Same as above, defaults to `"Customer"` |
| `transaction_date` | yes | ISO 8601 date or datetime (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`) | Becomes `Transaction.created_at` |
| `amount_usd` | yes | float, `0 < amount_usd <= settings.max_transaction_amount_usd` | Same ceiling already enforced on `POST /transactions` (`app/config.py`), reused here for consistency |
| `product_category` | no | string, max 60 chars | e.g. `"beverage"`, `"apparel"` — free text, not validated against the reward catalog's category list (uploaded data is independent of the reward catalog) |
| `product_name` | no | string, max 150 chars | e.g. `"Cold Brew 16oz"` |
| `channel` | no | string, one of `pos`/`online`/`mobile` | Defaults to `"pos"` if blank/omitted |
| `external_order_id` | no | string | If present, reused for idempotency exactly like the Shopify path — re-uploading the same file twice does not double-count rows that already exist |

One row = one purchase event = one new `Transaction` (multi-line-item
orders are out of scope — see §1's rejected-alternative note; a merchant
exporting an order with 3 SKUs uploads 3 rows).

### Validation rules & error handling

- **File-level (reject whole upload, `422`):** not a `.csv`/`text/csv`
  file; empty file; header row missing any of the three required columns
  (`customer_email`, `transaction_date`, `amount_usd`); more than
  `MAX_UPLOAD_ROWS = 20_000` data rows (DoS guard, same spirit as the
  existing `max_transaction_amount_usd` ceiling).
- **Row-level (skip the row, continue processing the rest — never abort
  the whole file for one bad row):** missing/unparseable
  `transaction_date`; missing/non-numeric/out-of-range `amount_usd`;
  missing `customer_email` or not a valid email shape (reuse
  `email-validator`, already a dependency); `external_order_id` that
  already exists on a `Transaction` for this merchant → counted as
  `duplicate`, not `failed`.
- **Response body** (`InsightsUploadResult` schema): `rows_received`,
  `rows_ingested`, `rows_skipped_duplicate`, `rows_failed`,
  `members_created`, `errors: list[{row: int, reason: str}]` (row = 1-based
  line number including header, so it maps directly to what the merchant
  sees in a spreadsheet). Always `200` if the file itself was parseable
  (partial success is normal and expected for messy real-world exports);
  only the file-level cases above return `422`/`400` with nothing ingested.
- **Atomicity:** all valid rows in one upload commit together in a single
  DB transaction (one `db.commit()` at the end) so a crash mid-file can't
  leave a half-applied upload; failed/duplicate rows are simply never
  added to the session, not rolled back individually.

### Does uploading mint real loyalty points? (`mint_points` param)

Uploaded rows are **treated as historical backfill data, not new live
purchases, by default (`mint_points=false`)**: they become real
`Transaction(type="earn", source="csv_upload", ...)` rows (so they feed the
future-value/next-best-product models and RFM), but `Member.points_balance`
is **not** incremented for them — a merchant backfilling six months of POS
history to make the report richer should not accidentally mint six months
of retroactive loyalty points. Passing `?mint_points=true` opts into also
calling the existing `app.services.ledger.earn_points()` per row (real
balance increase, same points-per-dollar math as every other earn path) —
for the (less common) case of a merchant uploading genuinely new,
not-yet-ledgered purchases. This is the one explicit design decision that
prevents the upload feature from silently corrupting real point balances,
called out because it's the kind of thing a coder implementing quickly
would get wrong by default (naively wiring the row loop straight into
`earn_points()`).

### New/changed files (§2)

- `app/db/models.py` — the two `Transaction` columns from §1.
- `app/schemas/insights.py` (new) — `InsightsUploadResult`,
  `InsightsUploadRowError` (see above), plus the future-value/next-best-
  product response schemas from §5.
- `app/services/csv_ingest.py` (new) — `parse_and_ingest_csv(db, merchant,
  raw_csv_bytes, mint_points: bool) -> InsightsUploadResult`, pure-ish
  service function (mirrors `app/services/shopify.py`'s
  `ingest_shopify_order` shape: takes a DB session + merchant + parsed
  input, returns a result, no HTTP concerns). Uses Python's stdlib `csv`
  module (`csv.DictReader`) — no new dependency needed.
- `app/api/insights.py` (new router, prefix `/api/v1/insights`) — houses
  `POST /upload` plus the GET endpoints from §5.
- `backend/scripts/fixtures/sample_product_transactions.csv` (new) — a
  small (~40-row), realistic sample file for a handful of the seeded demo
  merchant's real member emails, with plausible Northwind-Coffee-themed
  products (`"Cold Brew 16oz"` / `beverage`, `"Whole Bean Bag 12oz"` /
  `grocery`, `"Ceramic Mug"` / `merchandise`, etc.) — mirrors
  `shopify_order_create_sample.json`'s role from Batch 1: gives the tester
  and the demo a ready-made file so nobody has to hand-author a CSV to
  exercise the upload path.
- `backend/scripts/send_sample_csv_upload.py` (new, optional convenience
  script) — loads the fixture above and POSTs it to a running local
  instance, mirroring `send_sample_shopify_webhook.py`'s CLI shape
  (`--base-url`, `--merchant-email`, prints the `InsightsUploadResult`
  summary). Not strictly required (the frontend upload button covers the
  same path), but keeps a scriptable/CI-friendly way to demo it, matching
  the Batch 1 convention.

---

## 3. Future value model

### Honest framing (read this before the design below)

The seeded dataset is **~7,200 transactions over one continuous
window** for 620 members — synthetic, single-period, with no repeated
multi-cohort longitudinal history and no ground-truth "actual future
spend" label anyone collected in the real world. A model claiming to be a
rigorously trained, cross-validated CLV regressor here would be
overselling a demo. Following this codebase's own established pattern
(`churn_model.py`'s docstring: *"Deliberately not a trained classifier at
MVP scale... architected as a scoring function with clearly named,
swappable thresholds so a real supervised model can replace this later"*),
this feature uses a **backtested single-split regression with an explicit,
documented heuristic fallback** — genuinely trained on scikit-learn against
a real (if narrow) label derived from the seeded data, not fabricated, but
framed honestly as an MVP proof-of-concept, not production CLV.

### Concrete approach

**Step 1 — derive a real label via a single historical backtest split.**
Pick `cutoff = now - HOLDOUT_DAYS` (`HOLDOUT_DAYS = 45`, tuned to the
seeded data's date range so both the "training" and "holdout" windows have
enough transactions). For every member with at least one earn transaction
before `cutoff`:
- **Features** (all computed only from data *before* `cutoff`, reusing
  `churn_model.compute_rfm`'s exact recency/frequency/monetary
  definitions so this stays consistent with the existing churn signal
  rather than inventing a second RFM implementation): `recency_days`,
  `frequency`, `monetary`, `avg_order_value = monetary / max(frequency,
  1)`, `tenure_days = (cutoff - member.joined_at).days`, `tier_rank`
  (reuse `ledger.TIER_RANK`).
- **Label**: `future_spend = sum(amount_usd for earn txns in
  [cutoff, cutoff + HOLDOUT_DAYS])` — the actual realized spend in the
  holdout window. This is a real, not-fabricated target: it's exactly what
  a trained CLV model is supposed to predict, backtested against data that
  already happened.

**Step 2 — train.** `sklearn.linear_model.Ridge` (regularized linear
regression — simple, fast, interpretable coefficients, appropriate for
~500-row training data; explicitly *not* a gradient-boosted model, which
would be prone to overfitting at this sample size). Fit
`future_spend ~ features` across all eligible members. Log (not enforce) a
basic `R²`/`MAE` via `sklearn.model_selection.train_test_split` on this
same backtest set purely for the response payload's `model_quality` field
(see schema below) — surfaced to the merchant as a transparency signal,
not gated on.

**Step 3 — predict.** For every member, recompute the same feature set
using their **entire** available history (not the pre-cutoff subset — at
prediction time we want the freshest signal) and score with the trained
model, scaled to the requested `horizon_days` (default 90) via
`predicted_value = model.predict(features) * (horizon_days / HOLDOUT_DAYS)`.

**Step 4 — graceful fallback (per member, not global).** If a member has
zero pre-cutoff earn transactions (too new to have been part of training)
**or** the merchant has fewer than `MIN_TRAINING_MEMBERS = 30` eligible
members overall (training would be statistically meaningless), fall back
to an explicit heuristic, clearly labeled as such in the response
(`model_used: "heuristic"` vs `"trained"`):

```
predicted_future_value = avg_order_value
                          * (frequency / (LOOKBACK_DAYS / 30))   # monthly purchase rate
                          * (horizon_days / 30)
                          * retention_adjustment
```
where `retention_adjustment = 1 - (churn_risk_score / 100) * 0.7` (reuses
`churn_model.score_member_churn`'s existing output — a member already
flagged high-churn-risk gets their projection damped, capped so it never
zeroes out entirely at a 0.7 max dampening factor). This is exactly the
"recent average order value × predicted purchase frequency × retention-
adjusted horizon" heuristic the task brief suggested, used as the
documented fallback rather than the primary claim.

### New file

`app/ai/future_value.py` — `compute_future_value_features(db, member,
as_of) -> FVFeatures` (pure), `train_future_value_model(db, merchant_id) ->
FutureValueModel | None` (returns `None` if under
`MIN_TRAINING_MEMBERS`, letting the caller know to use the heuristic for
everyone), `predict_future_value(db, member, model, horizon_days) ->
FutureValueResult` (dataclass: `member_id`, `predicted_value`,
`horizon_days`, `model_used: Literal["trained","heuristic"]`,
`avg_order_value`, `monthly_purchase_rate`), `score_all_members_future_value(db,
merchant_id, horizon_days) -> list[FutureValueResult]` (trains once, reuses
across all members — same "compute once per request, not per member"
shape as `fraud_detector.run_fraud_detection`).

---

## 4. Next-best-product model

### Approach: item-based collaborative filtering over category (or product, if available)

1. Build a **member × category matrix** `M` where `M[i][j]` = total
   `amount_usd` member `i` spent in category `j`. Source, in priority
   order:
   - **If any `Transaction.product_category` is non-null for this
     merchant** (i.e. CSV data has been uploaded): use it directly —
     genuine purchase-pattern signal, category granularity (product-name
     granularity is used only as a secondary "representative example"
     label, not the CF substrate itself, since individual product names
     are too sparse for meaningful co-occurrence at demo scale).
   - **Else (out-of-the-box seeded data, no upload yet):** degrade to
     `Redemption` × `RewardCatalogItem.category` as the substrate — the
     same category-affinity signal `recommender.py` already reads, just
     aggregated across *all* members into a full item-based CF matrix
     instead of one member's own history. This is the concrete "degrade
     gracefully to category-level signals" path the task calls for — it
     is not empty/decorative, it produces real, if coarser,
     recommendations from data that already exists.
2. Compute **category-category cosine similarity**
   (`sklearn.metrics.pairwise.cosine_similarity` on `M.T`, i.e. treating
   each category as a vector of member-affinities — standard item-based
   CF) → similarity matrix `S`.
3. For a target member with category-affinity vector `v` (their row in
   `M`, normalized), score every category `c` they have **not** already
   engaged with (or engaged with below a low threshold) as
   `score(c) = Σ_j v[j] * S[j][c]` for `j != c` — the classic item-based CF
   "similarity-weighted sum of what they already like" formula. Rank
   descending, return top `N`.
4. **Next-best-product** (only when product-level data exists): within the
   top-ranked category, surface the single most-purchased-by-similar-
   members `product_name` in that category as the concrete product
   suggestion; when only category-level (redemption-derived) data exists,
   `next_best_product` is `null` and `next_best_category` is the
   deliverable (schema explicitly allows `product` to be optional so the
   frontend/tester know which granularity they're getting —
   `data_granularity: "product" | "category"` field, see §5).
5. **Cold-start member** (no purchase/redemption history at all): fall
   back to global category popularity (highest total spend/redemption
   count across all members) — same cold-start fallback shape
   `recommender.py` already documents for its own popularity signal.

### New file

`app/ai/next_best_product.py` — `build_affinity_matrix(db, merchant_id) ->
tuple[pd.DataFrame, Literal["product","category"]]` (the granularity flag
drives what the API reports), `recommend_next_best(db, member,
affinity_matrix, granularity, top_n) -> list[NextBestResult]` (dataclass:
`category`, `product_name: str | None`, `score`, `reason`).

---

## 5. API surface

New router `app/api/insights.py`, prefix `/api/v1/insights`, all endpoints
JWT-protected via `get_current_merchant` (dashboard-initiated, same as
`ai.py`):

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/insights/upload` | `POST` | CSV upload, `multipart/form-data`, `?mint_points=false` (§2) |
| `/api/v1/insights/future-value` | `GET` | All members, `?horizon_days=90` (default), returns `list[FutureValueOut]` |
| `/api/v1/insights/future-value/{member_id}` | `GET` | Single member detail, `404` if not found/wrong merchant |
| `/api/v1/insights/next-best-product/{member_id}` | `GET` | `?top_n=3` (default), `list[NextBestOut]`, `404` if member not found |
| `/api/v1/insights/report.csv` | `GET` | Combined export (see below) |

**Schemas (`app/schemas/insights.py`):**
```python
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
```

**`GET /api/v1/insights/report.csv`** — combines both models per member
(`member_id, first_name, last_name, email, tier, predicted_future_value,
horizon_days, model_used, next_best_category, next_best_product,
next_best_score`) into one CSV via FastAPI's `StreamingResponse` with
`media_type="text/csv"` and a `Content-Disposition: attachment;
filename="future_value_report.csv"` header — no new dependency (stdlib
`csv.writer` into an `io.StringIO`). PDF export is explicitly **not**
implemented — flagged as an optional stretch, matching the task brief's
"CSV is fine, PDF is a stretch."

`app/main.py` — `app.include_router(insights.router)`.

---

## 6. Demo path (no upload required)

Both endpoints must work immediately against the existing 620 seeded
members / ~7,200 transactions with **zero** upload step, per the task
brief's hard requirement:

- **Future value:** entirely derived from `Transaction.amount_usd` /
  `created_at` and `Member.joined_at`/`tier` — none of which depend on the
  new product columns. Training-eligibility check
  (`MIN_TRAINING_MEMBERS = 30`) comfortably passes at 620 seeded members,
  so `GET /insights/future-value` returns `model_used: "trained"` for the
  vast majority of members out of the box (any member with zero pre-cutoff
  activity, e.g. a very recently "joined" synthetic member, correctly
  falls back to `"heuristic"` — both paths are exercised by the seed data
  as-is, which is good for test coverage).
- **Next-best-product:** falls back to the `Redemption` ×
  `RewardCatalogItem.category` substrate (§4 step 1, second bullet) since
  no seeded `Transaction` has `product_category` set — returns
  `data_granularity: "category"` and `product_name: null` for every member
  out of the box. This is real (not decorative) — the seed script already
  generates enough redemption history for the CF matrix to be
  non-degenerate (confirmed by README: existing `recommender.py` category-
  affinity signal already relies on this same seeded redemption data being
  substantial enough to matter).
- **Upload is additive/optional:** after `POST /insights/upload` with the
  new `sample_product_transactions.csv` fixture (§2) against a handful of
  real seeded member emails, re-querying `next-best-product` for those
  specific members flips to `data_granularity: "product"` with a real
  `product_name` populated — a concrete, demoable "before/after" the
  tester can screenshot.

---

## 7. Frontend

**New page `frontend/src/pages/Insights.tsx`**, route `/insights`, nav
link added in `frontend/src/components/Layout.tsx` (same pattern as the
existing four nav items). Follows `Members.tsx`'s established conventions
exactly (sortable-column table, `useState`/`useEffect` fetch-on-mount, no
new state library):

- Table columns: Name, Tier, Predicted Future Value (`$`, with the
  `horizon_days` shown as a column subheader, e.g. "90-day"), a small
  "trained"/"heuristic" badge (reuses `RiskBadge.tsx`'s badge styling
  pattern, new variant), Next Best Category, Next Best Product (shows
  "—" when `null`, with a tooltip: *"Upload transaction data with product
  detail to unlock product-level suggestions"* — makes the degrade-
  gracefully path visible/explained to the merchant instead of silently
  blank).
- Sortable by name, tier, and predicted future value (mirrors `Members.tsx`
  `SortKey`/`toggleSort` pattern).
- **Upload button** — file input (`.csv` accept filter) → `POST
  /insights/upload` via a new `uploadInsightsCsv(file, mintPoints)`
  client function using `FormData` (first multipart use in this client —
  every existing call is JSON; note this explicitly since it's a new
  pattern in `client.ts`, not a copy-paste of existing request()). Shows a
  result summary toast/banner (`rows_ingested`, `rows_failed`,
  `members_created`, and up to the first 5 row errors if any) then
  refetches the table.
- **Download report button** — since `report.csv` needs the `Authorization`
  header (can't be a plain `<a href>` link, JWT isn't a cookie in this
  app), fetch as a blob and trigger a synthetic download
  (`URL.createObjectURL` + a temporary `<a>` click) — new
  `downloadInsightsReport()` client function.

**New `client.ts` additions:** `getFutureValue`, `getFutureValueForMember`,
`getNextBestProduct`, `uploadInsightsCsv`, `downloadInsightsReport`, plus
`FutureValueOut`/`NextBestOut`/`InsightsUploadResult` TS interfaces mirrored
1:1 from the Pydantic schemas in §5 (matching this file's own documented
convention: *"Endpoint shapes here are copied from the ACTUAL backend
implementation... not guessed"*).

**`App.tsx`:** add `<Route path="insights" element={<Insights />} />`
inside the existing authenticated `Layout` route group.

---

## Acceptance criteria (concrete, tester-verifiable)

1. `GET /api/v1/insights/future-value` against the seeded demo merchant
   (no upload) → `200`, one entry per member (620), every
   `predicted_future_value >= 0`, `model_used` is `"trained"` for the
   large majority and `"heuristic"` for any member with no pre-cutoff
   activity — both values must appear at least once in the seeded
   response (proves both code paths are live, not just the happy path).
2. `GET /api/v1/insights/future-value/{member_id}` for a real seeded
   member id → `200` matching that member's row from #1; unknown/foreign
   `member_id` → `404`.
3. `GET /api/v1/insights/next-best-product/{member_id}` for a seeded
   member (no upload yet) → `200`, `data_granularity: "category"`,
   `product_name: null`, non-empty `category`.
4. `POST /api/v1/insights/upload` with `sample_product_transactions.csv`
   → `200`, `rows_ingested` equal to the fixture's valid row count,
   `rows_failed: 0`; re-uploading the identical file a second time →
   `rows_skipped_duplicate` equal to the same count (idempotent via
   `external_order_id`), `rows_ingested: 0` on the second call.
5. After #4, `member.points_balance` for affected members is **unchanged**
   from before the upload (default `mint_points=false`) — explicit
   regression check for the "backfill doesn't corrupt real balances"
   decision in §2. Repeating #4 with `?mint_points=true` on a *fresh*
   (not-yet-uploaded) file **does** increase the affected members'
   balances by the expected points-per-dollar amount.
6. After #4, `GET /api/v1/insights/next-best-product/{member_id}` for one
   of the CSV's member emails → `data_granularity: "product"`, non-null
   `product_name`.
7. A CSV missing the `amount_usd` header entirely → `422`, nothing
   ingested. A CSV with 10 valid rows and 2 rows with an unparseable
   `transaction_date` → `200`, `rows_ingested: 10`, `rows_failed: 2`, and
   `errors` has exactly 2 entries with correct 1-based row numbers.
8. `GET /api/v1/insights/report.csv` → `200`, `Content-Type: text/csv`,
   parses as valid CSV with one data row per member and the documented
   columns.
9. All endpoints reject unauthenticated requests (`401`) and are scoped to
   the caller's own merchant (cross-merchant `member_id` → `404`, same
   pattern as every other member-scoped endpoint in this codebase).
10. Full backend suite: **69/69 pre-existing tests still pass unmodified**,
    plus new tests for `app/ai/future_value.py`, `app/ai/next_best_product.py`,
    `app/services/csv_ingest.py`, and `app/api/insights.py` (row-level CSV
    validation edge cases, idempotency, `mint_points` toggle, both
    future-value model paths, both next-best-product granularities).
11. Frontend: `/insights` route renders the table for the seeded demo
    merchant with zero console errors, sorting works on all three sortable
    columns, upload button round-trips against the fixture CSV and shows a
    result summary, download button produces a valid CSV file.

---

## Cross-cutting summary: risk to the existing 69 tests

| Change | Existing test(s) at risk | Required fix |
|---|---|---|
| Two new nullable `Transaction` columns (`product_category`, `product_name`) | `tests/test_fraud_detector.py:15` (only direct `Transaction(...)` construction in the suite, confirmed via grep) — uses kwargs only | None — additive nullable columns don't affect kwarg-only construction |
| New router (`insights.py`) / new files | None — purely additive, no existing router/file is modified | None |
| `app/ai/*` — no existing AI module is changed | `test_churn_model.py`, `test_recommender.py`, `test_fraud_detector.py` | None; future-value reuses `churn_model.compute_rfm`/`score_member_churn` by calling them, not modifying them |
| `app/services/ledger.py` — no change; `earn_points()` reused as-is via the optional `mint_points=true` path | `test_ledger.py`, `test_earn_concurrency.py` | None |
| `app/db/base.py` `create_all()` — no migration needed | none | None |

**Net:** zero modifications to any existing file's *behavior* (only
additive columns on `Transaction` and additive router registration in
`main.py`); all 69 existing tests are expected to keep passing unmodified.
This is a purely additive batch, lower schema-risk than Batch 1 (which
removed columns from `Merchant`) or the MVP's own concurrency fixes.

## Assumptions / explicitly flagged risks

- **Future-value's "trained" model is a single-split backtest on synthetic
  data, not a production CLV model** — explicitly surfaced to the merchant
  via `model_used` in every response rather than silently presented as
  more rigorous than it is. Flagged here again so the tester doesn't grade
  this against production-ML-quality bars; grade it against "is the method
  honestly labeled and does the fallback path actually work."
- **Multi-line-item orders are out of scope for CSV upload** (one CSV row
  = one `Transaction`, no basket/order grouping) — consistent with how the
  existing Shopify webhook already collapses `line_items` into a single
  earn transaction against `total_price`, so this isn't a new limitation
  relative to what the codebase already does elsewhere.
- **No new `UploadBatch`/audit table** for upload history — the upload
  endpoint is synchronous and returns its full result in the response
  body; if merchants need to look up "what did I upload last Tuesday"
  later, that's a natural but explicitly deferred follow-up.
- **`mint_points` default-false decision (§2)** is the single highest-risk
  design choice in this batch if a coder implements it differently than
  specified — flagged prominently in both §2 and acceptance criterion #5
  because getting this wrong (minting points by default) would be a real
  data-integrity bug against actual merchant point balances, not just a
  cosmetic issue.
