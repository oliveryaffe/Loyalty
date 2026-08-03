# Loyalty AI Framework

A B2B SaaS loyalty platform MVP with an embedded AI layer: personalized
reward recommendations, churn/attrition risk scoring, and fraud/anomaly
detection on top of a points-ledger loyalty engine. See `PLAN.md` in the
parent directory for the full architecture/assumptions writeup.

- **Backend**: Python 3.10+, FastAPI, SQLAlchemy, SQLite (default) — REST
  API at `http://127.0.0.1:8000`, interactive docs at `/docs`.
- **Frontend**: React + TypeScript + Vite merchant dashboard —
  `http://localhost:5173`, proxies `/api/*` to the backend in dev.
- **AI layer**: scikit-learn/pandas-based recommender, RFM churn scorer,
  and z-score/velocity fraud detector, all running in-process inside the
  FastAPI app (`backend/app/ai/`).

---

## 1. Prerequisites

- Python 3.10+
- Node.js 18+ and npm
- No external services required — SQLite is used by default, no Postgres/
  Docker/API keys needed to run the MVP end-to-end.

---

## 2. Backend setup

```bash
cd backend
python3 -m pip install -r requirements.txt
# optional: cp .env.example .env   (defaults work out of the box for local dev)

# Seed the database with a demo merchant, 620 members, ~7,200 transactions
# (including intentionally injected fraud-like patterns) and a reward catalog:
python3 scripts/seed_data.py

# Run the API (from the backend/ directory):
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The seed script prints demo merchant login credentials:

```
email:    demo@merchant.com
password: demo1234
```

Re-run `python3 scripts/seed_data.py` any time to reset to a clean,
deterministic dataset (it drops and recreates all tables by default; pass
`--no-reset` to add to the existing data instead).

API docs (Swagger UI): `http://127.0.0.1:8000/docs`
Health check: `GET http://127.0.0.1:8000/health`

### Run backend tests

```bash
cd backend
python3 -m pytest -q
```

208 tests: the 114 from Batches 1–2 (see below) plus 94 new Batch 3 tests
across GDPR (`test_gdpr.py`), Stripe billing (`test_billing.py`),
notifications (`test_notifications.py`), win-back campaigns
(`test_winback.py`), and A/B experiments (`test_experiments.py`), plus
regression coverage added to `test_team.py` and `test_recommender.py` for
bugs a tester pass found and a fix pass closed (see `TEST_REPORT_BATCH3.md`
/ `MANAGER_REVIEW_BATCH3.md`).

The original 114: 69 from Batch 1 (ledger math incl. concurrency regression
tests, transaction validation, all three original AI modules, DB URL
normalization, Shopify webhook ingestion, multi-user team accounts/roles)
plus 45 from Batch 2 — CSV ingestion (`test_csv_ingest.py`), the
future-value model (`test_future_value.py`), the next-best-product model
(`test_next_best_product.py`), and the `insights` API surface
(`test_insights_api.py`).

### Backend environment variables

Sourced via `backend/.env` (see `.env.example`) or process env vars —
nothing is hardcoded/committed:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./loyalty.db` | SQLAlchemy DSN. Point at a Postgres DSN for a real deployment — `postgres://` and bare `postgresql://` forms (e.g. what Railway injects) are auto-normalized to `postgresql+psycopg2://` at startup. |
| `JWT_SECRET_KEY` | dev-only placeholder | **Override this in any shared/deployed environment.** |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm. |
| `JWT_EXPIRE_MINUTES` | `720` (12h) | Team member session length. |

Note: there is no global `SHOPIFY_WEBHOOK_SECRET` env var. Shopify webhook
secrets are per-merchant (stored on `Merchant.shopify_webhook_secret` in the
DB, since different merchants would have different Shopify apps in a real
deployment) rather than a single global secret. The seed script sets a demo
value (`demo-shopify-secret-change-me`) on the seeded demo merchant and
prints it in its output — see §2a below.

---

## 2a. Batch 1 additions (Postgres, Shopify webhooks, team accounts)

**Persistent Postgres.** Point `DATABASE_URL` at a Postgres DSN (see the
`.env.example` comment) instead of the SQLite default. `postgres://` and
bare `postgresql://` DSNs (Railway/Heroku's injected form) are normalized to
`postgresql+psycopg2://` automatically. `scripts/seed_data.py --seed-if-empty`
creates any missing tables and seeds synthetic demo data only if the
merchants table is empty — safe to run on every container start, since it
no longer wipes existing data on redeploy/restart (see PLAN_BATCH1.md
Feature 1; wiring this into `backend/Dockerfile`'s CMD is deferred to the
infra/deployment stage, out of scope for this backend-only pass). SQLite
keeps working exactly as before for local dev/tests — nothing about the
default local workflow changes.

**Shopify-style webhook ingestion (sandboxed demo).** No real Shopify store
is needed to exercise this. With the backend running locally (seeded via
`python3 scripts/seed_data.py`, which prints the demo merchant's id and
webhook secret):

```bash
cd backend
python3 scripts/send_sample_shopify_webhook.py \
    --base-url http://127.0.0.1:8000 \
    --merchant-email demo@merchant.com \
    --secret demo-shopify-secret-change-me
```

This loads `scripts/fixtures/shopify_order_create_sample.json` (a
realistic, trimmed `orders/create` payload), signs it with a correct
`X-Shopify-Hmac-Sha256` HMAC using the given secret, POSTs it to
`POST /api/v1/webhooks/shopify/{merchant_id}/orders-create`, and prints the
created transaction id and the resolved member's new points balance.
Re-running it with the same fixture is a no-op (idempotent replay via
`external_order_id` — no double-credit); running it with the wrong
`--secret` fails with a `401` and a non-zero exit code. This endpoint is
intentionally **not** JWT-protected — Shopify's servers call it directly
with no login step, authenticating via the HMAC signature only.

**Multi-user merchant accounts with roles.** `POST /api/v1/auth/signup`
still creates a business account, now backed by a `Merchant` (business
entity) plus a first `TeamMember` (`role=admin`). JWTs now carry
`merchant_id` and `role` claims alongside `sub` (a `TeamMember` id, not a
`Merchant` id). New endpoints under `/api/v1/team`:

| Endpoint | Access | Purpose |
|---|---|---|
| `GET /api/v1/team` | any authenticated team member | List the merchant's team |
| `POST /api/v1/team/invite` | admin only | Add a teammate (sets their initial email+password directly — no email-delivery flow in this MVP) |
| `DELETE /api/v1/team/{id}` | admin only | Remove a teammate (`409` if it would remove the merchant's last admin) |
| `PATCH /api/v1/team/{id}/role` | admin only | Change a teammate's role (`409` if it would demote the last admin) |

The seed script now prints two accounts: the existing demo admin
(`demo@merchant.com` / `demo1234`) and a new demo non-admin
(`demo-member@merchant.com` / `demo1234`) for exercising role-gated
endpoints without needing the invite flow first. Frontend team-management
UI (invite/list/remove screens) is out of scope for this batch — backend
endpoints only.

---

## 2b. Batch 2 additions (future value, next-best-product, CSV upload)

**New `insights` API surface** (`app/api/insights.py`, prefix
`/api/v1/insights`, JWT-protected same as `ai.py`):

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/insights/upload` | `POST` | CSV upload, `multipart/form-data` field `file`, `?mint_points=false` (default) |
| `/api/v1/insights/future-value` | `GET` | All members, `?horizon_days=90` (default) |
| `/api/v1/insights/future-value/{member_id}` | `GET` | Single member, `404` if not found/wrong merchant |
| `/api/v1/insights/next-best-product/{member_id}` | `GET` | `?top_n=3` (default), `404` if not found |
| `/api/v1/insights/report.csv` | `GET` | Combined future-value + next-best-product export, one row per member |

**Predicted future value** (`app/ai/future_value.py`). A backtested
`sklearn.linear_model.Ridge` regression: features (recency/frequency/
monetary/avg-order-value/tenure/tier, reusing `churn_model`'s RFM
definitions) are computed from data *before* a `cutoff = now - 45 days`,
labeled with each member's *actual* realized spend in the 45 days after
that cutoff (a real backtest, not a fabricated label), then scored against
each member's full history at request time. Falls back to a documented
heuristic (`avg_order_value x monthly purchase rate x horizon x
churn-risk-damped retention adjustment`) per-member when a member has no
pre-cutoff purchase history, or merchant-wide when there are fewer than 30
eligible members to train on. Every response says which path produced it
(`model_used: "trained" | "heuristic"`) — this is an honestly-framed MVP
backtest against synthetic single-period data, not a production CLV model
(see the module docstring for the full framing, matching `churn_model.py`'s
existing "deliberately not overselling this" style).

**Next-best-product** (`app/ai/next_best_product.py`). Item-based
collaborative filtering over a member x category affinity matrix
(cosine similarity between categories, "similarity-weighted sum of what a
member already engages with"). Uses uploaded `Transaction.product_category`
data if any exists for the merchant (`data_granularity: "product"`,
surfaces a real representative `product_name`); otherwise degrades
gracefully to the existing `Redemption` x `RewardCatalogItem.category` data
the seeded demo already has (`data_granularity: "category"`,
`product_name: null`). Cold-start members with no history at all fall back
to global category popularity.

**CSV upload** (`POST /api/v1/insights/upload`). Header row required,
columns case-insensitive/order-independent:

| Column | Required | Notes |
|---|---|---|
| `customer_email` | yes | Matched against `Member.email`; auto-creates a new `Member` if not found |
| `customer_first_name` / `customer_last_name` | no | Only used when creating a new member; default `"Unknown"`/`"Customer"` |
| `transaction_date` | yes | ISO 8601 (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`) |
| `amount_gbp` | yes | `0 < amount_gbp <= max_transaction_amount_gbp` |
| `product_category` | no | Free text, max 60 chars |
| `product_name` | no | Free text, max 150 chars |
| `channel` | no | `pos` / `online` / `mobile`, defaults to `pos` |
| `external_order_id` | no | If present, re-uploading the same file is idempotent (duplicate rows are skipped, not re-ingested) |

File-level problems (not a `.csv`, empty, missing a required header, >20,000
rows) reject the whole upload with `422` and ingest nothing. Row-level
problems (bad date, bad amount, bad email) skip just that row and are
reported in the response body (`errors: [{row, reason}]`, 1-based row
numbers including the header) — one bad row never aborts the rest of the
file.

**Uploaded rows do NOT mint loyalty points by default**
(`mint_points=false`). They become real `Transaction` rows — so they feed
the future-value/next-best-product models and RFM — but
`Member.points_balance` is left untouched, because uploaded data is treated
as historical backfill, not a new live purchase. Pass `?mint_points=true`
to also credit real points via the normal `earn_points()` ledger path, for
the less common case of uploading genuinely new, not-yet-ledgered
purchases.

**Demo path — works with zero upload.** Both `future-value` and
`next-best-product` return real, non-degenerate results against the 620
seeded members out of the box (the seed script's `new_member` cohort — ~17
of the 620 — deliberately has no purchase history older than 45 days, so
the future-value heuristic fallback path is genuinely exercised alongside
the trained path, not just theoretical). To see the CSV upload path and the
`next-best-product` "category" → "product" granularity flip:

```bash
cd backend
python3 scripts/send_sample_csv_upload.py \
    --base-url http://127.0.0.1:8000 \
    --merchant-email demo@merchant.com \
    --merchant-password demo1234
```

This uploads `scripts/fixtures/sample_product_transactions.csv` (42 rows,
Northwind-Coffee-themed products against 6 real seeded member emails) and
prints the `InsightsUploadResult` summary. Re-running it is a no-op
(idempotent via `external_order_id`). Pass `--mint-points` to also credit
real points for the upload.

**Frontend**: new `/insights` page — sortable table (name, tier, predicted
90-day future value with a trained/heuristic badge, next-best category/
product), an upload button (with a "mint points" checkbox), and a
"Download report" button that fetches `report.csv` as an authenticated
blob download.

---

## 2c. Batch 3 additions (billing, notifications, win-back, A/B testing, GDPR)

**GDPR technical pass.** Two new merchant-admin endpoints on the existing
`members` router: `POST /api/v1/members/{id}/gdpr-erase` (idempotent —
anonymizes name/email in place and marks the member inactive; this is
**anonymization, not hard-delete**, so the merchant's own transaction/
redemption history and AI training data stay intact under a pseudonymous
id) and `GET /api/v1/members/{id}/gdpr-export` (one combined JSON export
covering that member's transactions, redemptions, fraud alerts, win-back
offers, and A/B experiment assignments — Art. 15/20 in one call). The
marketing site and dashboard now self-host Inter (`.woff2`, SIL OFL 1.1)
instead of loading it from Google's font CDN, which leaks visitor IPs to
Google — a real, court-tested GDPR issue, not a hypothetical one. Placeholder
`marketing/privacy.html` / `marketing/terms.html` pages exist (obvious
"needs a solicitor" skeleton, not drafted legal text) and are linked from
the marketing footer and the dashboard sidebar.

**Stripe billing.** Three tiers — Starter £49/mo (≤1,000 members), Growth
£149/mo (≤10,000 members, adds Shopify sync/Insights/notifications/
win-back), Scale £399/mo (unlimited, adds A/B testing) — via Stripe
Checkout + Billing Portal (`app/api/billing.py`, prefix `/api/v1/billing`:
`checkout-session`, `portal-session`, `subscription`, and a signature-
verified `webhook` endpoint handling the subscription lifecycle). Gating is
two-state, not a single locked/unlocked flag: `past_due` merchants get a
dismissible warning banner but stay fully functional (Stripe is still
retrying the card); `canceled`/`unpaid`/no-subscription merchants get a
`402` on most routes via a new `require_active_subscription` dependency.
Explicitly exempted from any lock, always reachable: auth, the billing
endpoints themselves, Shopify webhook ingestion (never lose a merchant's
sales data over a lapsed card), and the GDPR erase/export endpoints (never
paywall compliance). Requires real Stripe API keys to go live — see
`backend/.env.example` — the app runs fine locally without them (billing
endpoints return a clear `503` instead of a crash if unconfigured).

**Notifications.** Per-merchant configurable Slack webhook URL and/or
notification email (`/api/v1/settings/notifications`). No task scheduler
exists in this codebase, so alerts piggyback on the existing on-demand
`GET /ai/churn` / `GET /ai/fraud-alerts` recompute calls via FastAPI
`BackgroundTasks` — a real limitation: nothing fires until someone loads
the dashboard. A 24h cooldown (atomic UPDATE + rowcount check, the same
lost-update-safe pattern `ledger.py` uses for points) stops the same
escalation from re-notifying on every page load.

**Win-back campaigns.** A simple per-merchant rule
(`/api/v1/winback/rule`: "if churn risk ≥ X, offer reward Y") plus a
manual "run now" trigger and an automatic check that reuses notifications'
escalation-detection helper. `auto_trigger` defaults to **off** — mirrors
Batch 2's `mint_points=false` precedent, no free rewards go out without the
merchant opting in. A member is never offered twice.

**A/B testing.** `/api/v1/experiments` — an admin picks two existing
rewards and a traffic split; every currently-active member is immediately
and permanently hashed into a variant (stable across refetches), and
`recommend_for_member` steers each member toward their own variant (falling
back to the full catalog rather than ever returning zero recommendations,
if the exclusion would do that). A results view shows per-variant
assigned/redeemed counts and rates with a clearly-labeled *directional*
(not rigorous-statistics) winner indicator. Scale-tier feature per the
pricing table above, though tier-specific enforcement beyond the standard
active-subscription gate wasn't built this batch (noted in
`PLAN_BATCH3.md` as a deliberate scope call).

**Frontend**: new `/billing`, `/settings`, `/winback`, and `/experiments`
pages, plus a `SubscriptionGate` wrapper around the whole dashboard for the
soft/hard lock states described above.

Full writeup: `PLAN_BATCH3.md` (architect), `TEST_REPORT_BATCH3.md`
(adversarial QA — 1 high + 4 medium/low bugs found), `MANAGER_REVIEW_BATCH3.md`
(independent go/no-go, verdict: GO after the fix pass).

---

## 3. Frontend setup

```bash
cd frontend
npm install
npm run dev       # http://localhost:5173, proxies /api -> http://127.0.0.1:8000
```

Log in with the demo merchant credentials above (pre-filled on the login
screen). The dashboard has five pages: **Dashboard** (overview cards +
recent activity), **Members** (sortable list incl. churn-risk score/badge,
click a row for AI reward recommendations), **Rewards** (catalog + create
new reward), **Fraud Alerts** (anomaly feed with re-scan button), and
**Insights** (Batch 2 — predicted future value + next-best-product per
member, CSV upload, CSV report download).

### Build for production

```bash
cd frontend
npm run build      # tsc -b && vite build -> frontend/dist/
npm run preview    # serve the production build locally
```

### Frontend environment variables

| Variable | Default | Purpose |
|---|---|---|
| `VITE_API_BASE_URL` | `/api/v1` (relative) | Set to an absolute URL (e.g. `https://api.example.com/api/v1`) if the frontend is not served behind the same proxy/origin as the backend. Not needed for local dev — the Vite dev-server proxy in `vite.config.ts` already forwards `/api/*` to `http://127.0.0.1:8000`. |

---

## 4. Running the full system end-to-end

Two terminals, from the `loyalty-ai-framework/` directory:

```bash
# Terminal 1 — backend
cd backend
python3 -m pip install -r requirements.txt
python3 scripts/seed_data.py
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# Terminal 2 — frontend
cd frontend
npm install
npm run dev
```

Then open `http://localhost:5173`, log in with `demo@merchant.com` /
`demo1234`, and explore the dashboard. All frontend API calls go through
the Vite dev proxy to the backend on port 8000 — no CORS configuration
needed in dev (the backend's CORS middleware also explicitly allows
`http://localhost:5173` for non-proxied setups).

### Quick API sanity check (no frontend needed)

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@merchant.com","password":"demo1234"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -s http://127.0.0.1:8000/api/v1/members -H "Authorization: Bearer $TOKEN" | head -c 500
curl -s http://127.0.0.1:8000/api/v1/ai/churn -H "Authorization: Bearer $TOKEN" | head -c 500
curl -s http://127.0.0.1:8000/api/v1/ai/fraud-alerts -H "Authorization: Bearer $TOKEN" | head -c 500
```

---

## 5. Project structure

See `PLAN.md` (parent directory) §5 for the originally proposed structure.
Actual layout matches it closely:

```
loyalty-ai-framework/
├── backend/
│   ├── app/
│   │   ├── main.py, config.py
│   │   ├── db/ (base.py, models.py)
│   │   ├── api/ (auth, members, transactions, rewards, ai, team, webhooks, insights, deps)
│   │   ├── ai/ (recommender, churn_model, fraud_detector, future_value, next_best_product)
│   │   ├── schemas/ (auth, member, reward, transaction, ai, shopify, insights)
│   │   └── services/ (ledger, security, shopify, csv_ingest)
│   ├── scripts/ (seed_data.py, send_sample_shopify_webhook.py, send_sample_csv_upload.py,
│   │             fixtures/shopify_order_create_sample.json, fixtures/sample_product_transactions.csv)
│   └── tests/ (114 tests, all passing)
├── frontend/
│   ├── src/
│   │   ├── App.tsx, AuthContext.tsx, main.tsx, index.css
│   │   ├── api/client.ts        # typed REST client
│   │   ├── components/ (Layout, RiskBadge)
│   │   └── pages/ (Dashboard, Members, Rewards, FraudAlerts, Insights, Login)
│   └── dist/ (build output, gitignored)
└── README.md (this file)
```

---

## 6. Deviations from PLAN.md

- **Auth module includes a `/api/v1/auth/signup` endpoint** in addition to
  `/login` — not explicitly called out in PLAN.md §5 but needed to create
  the demo merchant account programmatically (the seed script itself
  inserts the merchant row directly rather than calling signup, but the
  endpoint exists for completeness/dev convenience and is covered by
  `tests/test_auth.py`).
- **`AuthContext.tsx` and `client.ts` live at `frontend/src/` and
  `frontend/src/api/`** rather than under a `frontend/src/auth/` folder —
  a single top-level auth context file was simpler than a whole directory
  for one file, and PLAN.md §5 didn't mandate an `auth/` subfolder.
- **No React Testing Library frontend tests were added** (PLAN.md §4
  mentions RTL as the intended frontend test tool). The `frontend/tests/`
  directory exists but is empty. Given the AI layer (the differentiator)
  already has thorough backend unit tests and the frontend is a fairly
  thin REST-client dashboard, this was deprioritized versus finishing all
  P0–P2 required scope; flagged as follow-up for the tester stage.
- **Dynamic reward-value optimization (P3 stretch)** was not implemented,
  as explicitly allowed by PLAN.md §5/§6 (optional, not required for MVP
  acceptance).
- **Members list churn scoring is computed on-demand per request**
  (`include_churn=true` query param, default on) rather than
  pre-computed/cached — fine at MVP/620-member scale, called out here in
  case it needs to become an async/background job at larger scale.
- **Sandbox note (not a code issue):** during this build/verification
  pass, `npm install` run directly inside the mounted project directory
  intermittently hit filesystem-level `ENOTEMPTY`/timeout errors specific
  to this sandbox's network-mounted working directory (large numbers of
  small-file writes over a slow mount). The frontend was verified to
  `npm install` and `npm run build` cleanly with zero TypeScript/build
  errors when run against the same `package.json`/source from a local
  filesystem path; a `package-lock.json` generated from that successful
  install is committed here. This should not affect a normal local
  developer machine or CI runner.

**Deviations from PLAN_BATCH1.md (this batch's own plan doc, backend/PLAN_BATCH1.md):**
- **`backend/Dockerfile`'s CMD was initially left unchanged** in the first
  coder pass (still unconditionally ran `python scripts/seed_data.py`,
  reset mode) even though PLAN_BATCH1.md Feature 1 calls switching it to
  `scripts/seed_data.py --seed-if-empty` the single highest-risk item in
  that feature. `TEST_REPORT_BATCH1.md` correctly flagged this as
  CRITICAL-1 — the actual shipped artifact still wiped the database on
  every restart, defeating the entire point of Feature 1. **This has since
  been fixed** (see "Two critical bugs from TEST_REPORT_BATCH1.md" below):
  the Dockerfile CMD now reads
  `python scripts/seed_data.py --seed-if-empty && uvicorn ...`.
- **`ShopifyOrderWebhook.customer` is a required field, not `ShopifyCustomer
  | None`** as one line of PLAN_BATCH1.md's terse field list literally
  reads. This was necessary for internal consistency with the rest of that
  same plan section: the webhook handler description explicitly calls for
  "422 on schema mismatch, e.g. missing `customer`," and
  `ingest_shopify_order`'s design assumes `payload.customer.email` always
  exists (used both to resolve/create the `Member`). Making `customer`
  optional would silently break both of those and violate acceptance
  criterion 7 (missing `customer` → 422). Sub-fields within `customer`
  other than `email` remain optional, matching the plan.

Two issues from the tester's report (`TEST_REPORT.md` §c) were fixed in a
follow-up pass:

- **CRITICAL — redemption race condition (TOCTOU/lost update).**
  `redeem_reward()` (`backend/app/services/ledger.py`) used to check
  `member.points_balance >= reward.points_cost` in Python and then write
  the debit as a separate step; concurrent requests could all pass the
  check before any of them wrote, letting a member redeem far more than
  their actual balance (tester reproduced 10/10 concurrent requests
  succeeding against a 100-point balance). Fixed by replacing the
  check-then-write with a single atomic
  `UPDATE members SET points_balance = points_balance - :cost WHERE id =
  :id AND points_balance >= :cost` statement and checking `rowcount`; only
  one concurrent caller can ever match the WHERE clause, so the rest are
  correctly rejected with `InsufficientBalanceError` (400) regardless of
  how many requests race in at once. Also bumped the SQLite connection
  `timeout` (`backend/app/db/base.py`) so a burst of concurrent writers
  waits and serializes cleanly instead of surfacing `database is locked`
  as a raw 500. Regression test:
  `backend/tests/test_redemption_concurrency.py` — fires 10 truly
  concurrent HTTP requests (via a live background `uvicorn` server, so
  each gets its own DB session/connection) against a member with exactly
  100 points and a 100-point reward, and asserts exactly 1 succeeds, 9 are
  rejected with 4xx, and the final balance is exactly 0.
- **MAJOR — no upper bound on transaction amount.** `TransactionCreate`
  (`backend/app/schemas/transaction.py`) only enforced `amount_gbp > 0`,
  so a single call could mint an arbitrary number of points. Added a
  documented ceiling, `Settings.max_transaction_amount_gbp` (default
  £50,000, `backend/app/config.py`), enforced via `Field(le=...)` on the
  schema. Covered by
  `test_transaction_amount_over_max_is_rejected`/`test_transaction_amount_at_max_is_accepted`
  in `backend/tests/test_transactions.py`.

Both fixes are covered by the full backend test suite (42/42 passing:
the original 39 plus the 3 new tests above).

Two critical bugs from the Batch 1 tester's report (`TEST_REPORT_BATCH1.md`
§c) were fixed in a follow-up pass:

- **CRITICAL-1 — the Postgres persistence fix was never actually
  deployed.** `backend/Dockerfile`'s CMD still called
  `python scripts/seed_data.py` with no flag, which runs
  `Base.metadata.drop_all()` + `create_all()` (a full reset) on every
  single container start — including a plain restart with no code change —
  silently wiping the database on every deploy despite `--seed-if-empty`
  (Feature 1's actual fix) being fully implemented and unit-tested. Fixed
  by changing the CMD to
  `python scripts/seed_data.py --seed-if-empty && uvicorn app.main:app ...`.
  Verified by running `seed_data.py --seed-if-empty` twice in a row
  against the same SQLite file: the first run (empty DB) fully seeds
  (merchant id, 620 members); the second run (simulating a restart)
  correctly no-ops — same merchant `id`/`created_at`, same member count —
  proving redeploys no longer reset real data. Docker itself isn't
  available in this sandbox to build the image directly, so this was
  verified by (a) directly reading the Dockerfile CMD line and (b) running
  the exact seed-twice repro described above and in
  `TEST_REPORT_BATCH1.md` CRITICAL-1/§(e).
- **CRITICAL-2 — `earn_points()` had the same read-modify-write race as
  the redemption bug above, exposed via concurrent Shopify webhook
  replays.** Two separate bugs, both fixed:
  1. `earn_points()` (`backend/app/services/ledger.py`) credited
     `member.points_balance += points` in Python — a lost-update race,
     same shape as the pre-fix `redeem_reward()`. Fixed the same way: a
     single atomic
     `UPDATE members SET points_balance = points_balance + :points WHERE
     id = :id` statement, so concurrent credits for the same member both
     durably apply instead of one clobbering the other's read.
  2. The Shopify webhook idempotency check
     (`ingest_shopify_order()`, `backend/app/services/shopify.py`) was a
     plain SELECT-then-INSERT with no DB-level backing — a TOCTOU race
     under concurrent redelivery of the same `external_order_id`. Fixed
     by adding a `unique=True` constraint on
     `Transaction.external_order_id` (`backend/app/db/models.py`, NULLs
     from non-webhook transactions are unaffected) and catching the
     resulting `IntegrityError` in `ingest_shopify_order()` as the
     authoritative "already processed" signal — the SELECT remains as a
     cheap fast-path check only, never the source of truth.
  Regression tests: `backend/tests/test_earn_concurrency.py` — (1) fires
  25 truly concurrent identical webhook deliveries (same
  `external_order_id`, live `uvicorn` server, `threading.Barrier`-forced
  simultaneity) and asserts exactly 1 succeeds (201), the rest are
  `duplicate_ignored` (200), exactly 1 `Transaction` row exists for that
  `external_order_id`, and the member's `points_balance` reflects exactly
  one earn — not doubled, not corrupted; (2) fires 20 concurrent
  *distinct* earns for the same member (no `external_order_id` involved)
  and asserts the final balance is the exact sum of all 20, proving
  `earn_points()` itself no longer drops concurrent increments. Also
  independently re-reproduced the tester's own forced two-thread
  barrier-synchronized interleaving (their CRITICAL-2 method 2) directly
  against `ingest_shopify_order()`: previously produced 2 `Transaction`
  rows and a corrupted balance of 49 instead of 98; against the fixed code
  it now consistently (5/5 runs) produces exactly 1 `Transaction` row and
  the correct balance.

Both fixes are covered by the full backend test suite (69/69 passing: the
67 from the Batch 1 tester's fresh run plus the 2 new concurrency tests
above).

**Deviations from PLAN_BATCH2.md (this batch's own plan doc,
`PLAN_BATCH2.md`):**

- **`compute_future_value_features` does not literally call
  `churn_model.compute_rfm(now=cutoff)` during training**, despite the
  plan's prose saying to reuse "compute_rfm's exact recency/frequency/
  monetary definitions". `compute_rfm` has no upper-bound time filter (its
  `window_start` is a lower bound only, and its recency figure always uses
  `member.last_activity_at`, the member's true present-day last activity,
  not their activity "as of" an earlier cutoff) — calling it with
  `now=cutoff` at training time would leak post-cutoff transactions into
  supposedly pre-cutoff features, exactly the label leakage a backtest
  exists to prevent. `app/ai/future_value.py`'s `_rfm_as_of` reimplements
  the same recency/frequency/monetary *formula* (same `LOOKBACK_DAYS`
  window, same "earn transactions only" definition) but properly bounded
  to `created_at <= as_of`. At prediction time (`as_of=now`) this is
  equivalent to `compute_rfm`, and `compute_rfm`/`score_member_churn` are
  called directly for the heuristic fallback, exactly as the plan
  describes. See the module docstring for the full explanation.
- **`app/schemas/insights.py`'s `FutureValueOut` has no `model_quality`
  field.** The plan's prose in §3 step 2 mentions logging R²/MAE "for the
  response payload's `model_quality` field", but the literal schema block
  in §5 (which is what other parts of the plan call "exact API endpoints")
  does not include one. Implemented the literal schema as written; R²/MAE
  are still computed internally (`FutureValueModel.r2`/`.mae` in
  `future_value.py`) for potential future use/logging, just not exposed
  over the API.
- **`scripts/seed_data.py` gained a small new `"new_member"` synthetic
  cohort (~3% of members, carved out of the `"average"` cohort's weight,
  loyal/lapsing/at_risk unchanged).** The plan's §6 demo-path claim — "any
  member with zero pre-cutoff activity ... both paths are exercised by the
  seed data as-is" — turned out to be false against the actual Batch 1 seed
  data once implemented and tested: every existing cohort's transaction
  history spans widely enough (and cohorts like `lapsing`/`at_risk` are
  deliberately *all* old) that, empirically, zero members organically
  landed with all-activity-inside-the-last-45-days (verified by running
  `score_all_members_future_value` against the original seed data before
  this change: 620/620 came back `"trained"`, 0 `"heuristic"`). Since
  acceptance criterion 1 explicitly requires both `model_used` values to
  appear in the out-of-the-box seeded response, and getting the demo path
  actually working (not just claimed) is this project's stated bar, a
  small cohort of genuinely brand-new members (joined 5-40 days ago, 1-3
  purchases all within the last ~35 days) was added so the heuristic
  fallback is genuinely, not theoretically, exercised. Verified live: 620
  members score with a 603/17 trained/heuristic split, matching the new
  cohort size. `test_churn_model.py`'s existing loyal/lapsing cohort-size
  assertions (`> 20` each) still pass comfortably (88/107 members).
- **The Insights page's Tier column is populated via a second call to the
  existing `GET /members` endpoint**, merged client-side by `member_id`.
  `FutureValueOut` (per the plan's own literal schema in §5) has no `tier`
  field, but the plan's §7 frontend section calls for a Tier column and a
  tier sort — implemented by fetching `listMembers()` alongside
  `getFutureValue()` rather than dropping the column or leaving it fake.
- **Next-best-product is not fetched eagerly for all 620 members up
  front.** The plan's API surface (§5) deliberately has no merchant-wide
  bulk endpoint for next-best-product (unlike future-value/churn, which
  do) — firing 620 individual requests synchronously on page load would be
  a poor demo experience. `Insights.tsx` renders the future-value table
  immediately (one bulk call) and fills in the Next Best Category/Product
  columns progressively via a bounded-concurrency (8 at a time) background
  fetch loop, matching the per-member-only API shape the plan specifies.

---

## 7. Explicitly out of scope (per PLAN.md §6)

Multi-tenant billing, real POS/e-commerce integrations (Shopify/Square),
production-grade auth (SSO/OAuth), horizontal scaling/deployment infra,
and dynamic reward-value optimization are all documented future work, not
MVP gaps.
