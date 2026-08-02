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

42 tests cover ledger math (earn/redeem/balance rules, including a
concurrency regression test — see §6a), transaction validation, and all three AI
modules (recommender, churn model, fraud detector) against the deterministic
seeded/fixture data.

### Backend environment variables

Sourced via `backend/.env` (see `.env.example`) or process env vars —
nothing is hardcoded/committed:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./loyalty.db` | SQLAlchemy DSN. Point at a Postgres DSN for a real deployment. |
| `JWT_SECRET_KEY` | dev-only placeholder | **Override this in any shared/deployed environment.** |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm. |
| `JWT_EXPIRE_MINUTES` | `720` (12h) | Merchant admin session length. |

---

## 3. Frontend setup

```bash
cd frontend
npm install
npm run dev       # http://localhost:5173, proxies /api -> http://127.0.0.1:8000
```

Log in with the demo merchant credentials above (pre-filled on the login
screen). The dashboard has four pages: **Dashboard** (overview cards +
recent activity), **Members** (sortable list incl. churn-risk score/badge,
click a row for AI reward recommendations), **Rewards** (catalog + create
new reward), and **Fraud Alerts** (anomaly feed with re-scan button).

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
│   │   ├── api/ (auth, members, transactions, rewards, ai, deps)
│   │   ├── ai/ (recommender, churn_model, fraud_detector)
│   │   ├── schemas/ (auth, member, reward, transaction, ai)
│   │   └── services/ (ledger, security)
│   ├── scripts/seed_data.py
│   └── tests/ (42 tests, all passing)
├── frontend/
│   ├── src/
│   │   ├── App.tsx, AuthContext.tsx, main.tsx, index.css
│   │   ├── api/client.ts        # typed REST client
│   │   ├── components/ (Layout, RiskBadge)
│   │   └── pages/ (Dashboard, Members, Rewards, FraudAlerts, Login)
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

---

## 6a. Known issues fixed (post-tester pass)

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
  (`backend/app/schemas/transaction.py`) only enforced `amount_usd > 0`,
  so a single call could mint an arbitrary number of points. Added a
  documented ceiling, `Settings.max_transaction_amount_usd` (default
  $50,000, `backend/app/config.py`), enforced via `Field(le=...)` on the
  schema. Covered by
  `test_transaction_amount_over_max_is_rejected`/`test_transaction_amount_at_max_is_accepted`
  in `backend/tests/test_transactions.py`.

Both fixes are covered by the full backend test suite (42/42 passing:
the original 39 plus the 3 new tests above).

---

## 7. Explicitly out of scope (per PLAN.md §6)

Multi-tenant billing, real POS/e-commerce integrations (Shopify/Square),
production-grade auth (SSO/OAuth), horizontal scaling/deployment infra,
and dynamic reward-value optimization are all documented future work, not
MVP gaps.
