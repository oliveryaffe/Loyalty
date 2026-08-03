# PLAN_BATCH1.md — Ledgerly, Feature Batch 1

**Scope:** exactly three additions on top of the existing, already-deployed
MVP (README.md/TEST_REPORT.md/MANAGER_REVIEW.md in this directory describe
what already exists and passed review): (1) persistent Postgres, (2)
Shopify-style webhook transaction ingestion (sandboxed demo), (3) multi-user
merchant accounts with roles. No other behavior changes are in scope for
this batch. There is no `PLAN.md` file in this directory (only in a parent
directory referenced by README.md, not present here) — this document is
self-contained and reads directly off the actual current code in
`backend/app/*` and `backend/tests/*`.

**Baseline being extended:** FastAPI + SQLAlchemy 2.0 + SQLite
(`backend/app/db/base.py`, `app/db/models.py`), single-merchant JWT auth
(`app/api/auth.py`, `app/api/deps.py`, `app/services/security.py`),
transaction ingestion via `POST /api/v1/transactions`
(`app/api/transactions.py`, `app/services/ledger.py`), 42 passing pytest
tests (`backend/tests/`), deployed on Railway via `backend/Dockerfile` /
`Procfile`.

**Global assumption for this batch:** this is still a pre-launch demo app
with only synthetic seed data (no real customer data exists anywhere). All
three features below assume a **clean cutover** is acceptable — i.e. it is
fine for schema changes to be applied via `Base.metadata.create_all()`
against a fresh database (Postgres or a freshly reseeded SQLite file)
rather than via in-place ALTER/Alembic migrations. Alembic is explicitly
deferred to a future batch (flagged in Risks, Feature 1).

---

## Feature 1 — Persistent Postgres database with a volume

### Problem
`DATABASE_URL` defaults to `sqlite:///./loyalty.db`, and the deployed
`backend/Dockerfile` CMD is:
```
python scripts/seed_data.py && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```
`scripts/seed_data.py` **drops and recreates all tables by default**
(`reset=True`). Combined with an ephemeral container filesystem, every
redeploy/restart currently wipes all data. This is the actual bug being
fixed, not just "SQLite doesn't have a volume."

### Changes

**`backend/requirements.txt`**
- Add `psycopg2-binary>=2.9,<3` (sync driver, matches the existing sync
  SQLAlchemy engine/session setup in `app/db/base.py` — no need to move to
  async SQLAlchemy or `psycopg` v3 for this batch).

**`backend/app/config.py`**
- Add a `field_validator` (or a plain `__init__`/`model_post_init` check)
  on `database_url` that rewrites Railway/Heroku-style `postgres://` DSNs
  to `postgresql+psycopg2://` — SQLAlchemy 2.x rejects the bare
  `postgres://` scheme, and Railway's Postgres plugin injects
  `DATABASE_URL` in that exact form. Example:
  ```python
  @field_validator("database_url")
  @classmethod
  def _normalize_db_url(cls, v: str) -> str:
      if v.startswith("postgres://"):
          return v.replace("postgres://", "postgresql+psycopg2://", 1)
      if v.startswith("postgresql://"):
          return v.replace("postgresql://", "postgresql+psycopg2://", 1)
      return v
  ```
- No other settings need to change (`max_transaction_amount_usd`, JWT
  settings, etc. are all DB-agnostic already).

**`backend/app/db/base.py`**
- `connect_args` logic is already conditional on `startswith("sqlite")` —
  no change needed there.
- Add `pool_pre_ping=True` to `create_engine(...)` (harmless for SQLite,
  important for Postgres: Railway's managed Postgres can silently drop idle
  connections, and without pre-ping the next request gets a raw
  `OperationalError` instead of a clean reconnect).
- `init_db()` stays `Base.metadata.create_all(bind=engine)` — non-destructive
  by design (only creates missing tables), consistent with the clean-cutover
  assumption. Note in a code comment (already partially present) that
  Alembic is deferred, not forgotten.

**`backend/Dockerfile` (must fix — this is the actual persistence bug)**
- Do **not** unconditionally run `python scripts/seed_data.py` (reset mode)
  on every container start once persistence matters. Change
  `scripts/seed_data.py` to support an idempotent bootstrap mode, e.g. a new
  `--seed-if-empty` flag (or a `seed_if_empty()` function) that checks
  `db.query(Merchant).first()` and only seeds when the merchants table is
  empty; otherwise it's a no-op. Update the Dockerfile CMD to:
  ```
  CMD ["sh", "-c", "python scripts/seed_data.py --seed-if-empty && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
  ```
  This preserves the "fresh environment bootstraps itself" convenience
  (first deploy against a brand-new Postgres still seeds automatically) while
  making redeploys/restarts safe once real data (or a second admin invited
  in Feature 3, or Shopify-ingested transactions from Feature 2) exists.
  **This is the single highest-risk item in Feature 1** — without it,
  moving to Postgres does not actually fix the reported problem.

**`.env.example` / README**
- Add a commented example: `DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/railway`.

### Migration strategy (explicit assumption)
No migration of existing SQLite data is planned or needed. Clean cutover:
provision a fresh Railway Postgres, point `DATABASE_URL` at it, let the
bootstrap seed logic populate synthetic demo data once. The old SQLite file
(ephemeral on Railway already) is simply abandoned. If this assumption is
wrong (i.e. someone has manually entered real data into the current
deployment that must be preserved), that is a separate, out-of-band data
migration task not covered by this batch.

### Infra note (for the infra stage, task #25)
Railway's managed Postgres **plugin/service already provides its own
durable storage** — you attach a Postgres service to the project and it
gets `DATABASE_URL` wired in; you do not need to separately create/attach a
Railway "Volume" resource to the *backend* service for this. A Volume
resource would only be needed if we wanted to keep SQLite and give the
existing backend container a persistent disk. Since we're moving to
Postgres, provisioning the Postgres service *is* the durable-storage step —
flagging this so the infra stage doesn't do redundant/wrong work.

### Acceptance criteria
1. With `DATABASE_URL` pointed at a Postgres instance, `uvicorn app.main:app`
   starts with no errors and `GET /health` returns `200 {"status":"ok"}`.
2. `postgres://...` and `postgresql://...` DSN forms are both normalized to
   a working `postgresql+psycopg2://...` engine — covered by a new unit test
   (e.g. `backend/tests/test_config.py::test_database_url_normalization`)
   that constructs `Settings(database_url="postgres://u:p@h/d")` and asserts
   the normalized value, without requiring an actual live Postgres.
3. `python scripts/seed_data.py` run once against a real/local Postgres
   produces the same counts as SQLite today (620 members, ~7,200
   transactions, reward catalog) — manual verification step for the tester
   (requires a real or local Postgres instance; document the exact command).
4. **Restart-durability check (the actual bug fix):** start the app against
   Postgres with `--seed-if-empty`, note the member count and a specific
   transaction id, restart the process (simulating a Railway redeploy)
   without wiping the DB, and confirm the same member count and transaction
   id are still present — i.e. redeploying no longer resets data.
5. Starting fresh against an *empty* Postgres still auto-seeds on first boot
   (bootstrap convenience preserved).
6. All 42 existing pytest tests still pass unmodified — `backend/tests/conftest.py`
   already forces `DATABASE_URL` to a SQLite temp file before any app import,
   so the test suite is unaffected by this change and continues running
   against SQLite (explicitly not switched to Postgres for the test suite in
   this batch — flagged as a possible future improvement, not required now).
7. `pip install -r requirements.txt` succeeds with `psycopg2-binary` added,
   on both the CI/dev environment and the `python:3.11-slim` Docker image
   used for deployment (binary wheel avoids needing `libpq-dev` at build
   time — confirm no build errors in the Docker build step).

---

## Feature 2 — Shopify-style webhook transaction ingestion (sandboxed)

### Design
Real Shopify `orders/create` webhooks POST a JSON body shaped like Shopify's
Order resource (subset of real field names used here): top-level `id`
(Shopify's own numeric order id), `order_number`, `created_at`,
`total_price` (string, e.g. `"49.95"`), `currency`, `financial_status`,
`line_items` (array), and a nested `customer` object with `id`, `email`,
`first_name`, `last_name`. Authentication is via the
`X-Shopify-Hmac-Sha256` request header: Shopify computes
`base64(HMAC-SHA256(raw_request_body, shared_secret))` and the receiver
must recompute and compare it — **not** a bearer JWT, since Shopify's
servers call this endpoint directly with no login step. There is no real
Shopify store to connect to, so this batch also ships (a) a script that
sends a realistic sample payload with a correctly computed HMAC signature
against a running local/deployed instance, and (b) a fixture JSON file both
the script and the tests reuse.

### New/changed files

**`backend/app/db/models.py`**
- `Merchant`: add `shopify_webhook_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)`
  and `shopify_shop_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)`.
- `Transaction`: add `external_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)`
  (Shopify's order `id`, used for idempotency/dedup) and
  `source: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)`
  (`"manual"` for existing `POST /transactions` calls, `"shopify"` for
  webhook-ingested ones). Both nullable/defaulted so no existing row is
  invalidated.

**`backend/app/schemas/shopify.py` (new)**
- `ShopifyCustomer` (id, email, first_name, last_name — all optional except
  email) and `ShopifyOrderWebhook` (id, order_number, created_at,
  total_price: str, currency, financial_status, customer:
  ShopifyCustomer | None, line_items: list = []). Use
  `model_config = ConfigDict(extra="ignore")` so a real, much larger Shopify
  payload (hundreds of fields) validates fine — we only read what we need.

**`backend/app/services/shopify.py` (new)**
- `verify_shopify_hmac(raw_body: bytes, signature_header: str | None, secret: str) -> bool`:
  computes `base64.b64encode(hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()).decode()`
  and compares with `hmac.compare_digest`. Returns `False` on missing header
  or empty secret (never treats "no secret configured" as "skip
  verification").
- `ingest_shopify_order(db, merchant, payload: ShopifyOrderWebhook) -> tuple[Transaction | None, bool]`
  (`bool` = `is_duplicate`):
  - Idempotency: look up an existing `Transaction` with
    `external_order_id == str(payload.id)` joined to this merchant's
    members; if found, return `(None, True)` — no double-credit on webhook
    redelivery (this also closes the "no idempotency protection" MINOR gap
    flagged in `TEST_REPORT.md`).
  - Resolve `Member`: match by `(merchant_id, email)`; if none exists,
    auto-create one from `customer.first_name`/`last_name`/`email`
    (real Shopify orders can come from first-time customers).
  - Parse `total_price` to `float`, call the **existing**
    `app.services.ledger.earn_points(db, member, amount_usd, channel="shopify")`
    (no ledger logic is duplicated — this is the one required integration
    point per the task).
  - Stamp `external_order_id`/`source="shopify"` on the created `Transaction`
    before `db.flush()`.

**`backend/app/api/webhooks.py` (new)**
- `POST /api/v1/webhooks/shopify/{merchant_id}/orders-create`
  — **not** behind `get_current_merchant`/JWT (real Shopify webhooks carry
  no user session). Reads the raw body via `await request.body()` *before*
  any Pydantic parsing (required for HMAC verification — this is a
  deliberate deviation from the rest of the codebase's typed-body-param
  style, called out as a webhook-specific pattern). Looks up the
  `Merchant` by path id (404 if unknown), verifies HMAC against
  `merchant.shopify_webhook_secret` (401 if missing/invalid — and 401 if
  the merchant has no secret configured yet, not "accept anything"),
  then `json.loads` + validates against `ShopifyOrderWebhook` (422 on
  schema mismatch, e.g. missing `customer`), then calls
  `ingest_shopify_order`. Returns `201` with `TransactionOut` on success,
  `200 {"status": "duplicate_ignored"}` on idempotent replay.

**`backend/scripts/fixtures/shopify_order_create_sample.json` (new)**
- A realistic, real-field-shaped sample `orders/create` payload (trimmed to
  the fields above plus a couple of representative `line_items` for
  realism), used by both the demo script and the pytest tests.

**`backend/scripts/send_sample_shopify_webhook.py` (new — the demo path)**
- CLI script, following the existing `seed_data.py` convention (argparse,
  runnable standalone). Usage:
  ```
  python scripts/send_sample_shopify_webhook.py \
      --base-url http://127.0.0.1:8000 \
      --merchant-email demo@merchant.com \
      --secret demo-shopify-secret-change-me
  ```
  Loads the fixture JSON, computes the correct HMAC signature with the
  given secret, POSTs it to
  `{base-url}/api/v1/webhooks/shopify/{merchant_id}/orders-create` (looks
  up `merchant_id` via a lightweight local DB read or accepts
  `--merchant-id` directly), and prints the resulting transaction id and
  the member's new points balance. Exit code 0 on success, non-zero with a
  clear message on any failure (bad signature, non-2xx, etc.) — this is
  the concrete, no-real-Shopify-account-needed way the tester verifies the
  feature end-to-end.

**`backend/scripts/seed_data.py`**
- Set `shopify_webhook_secret="demo-shopify-secret-change-me"` and a
  placeholder `shopify_shop_domain` on the demo merchant (same pattern as
  the existing hardcoded `DEMO_MERCHANT_PASSWORD` constant); print it in the
  seed script's output block alongside the existing login credentials so
  it's discoverable the same way.

**`backend/app/main.py`**
- `app.include_router(webhooks.router)`.

### Acceptance criteria
1. POSTing the sample fixture with a correctly computed HMAC (using the
   merchant's configured secret) to
   `/api/v1/webhooks/shopify/{merchant_id}/orders-create` returns `201`,
   creates exactly one `Transaction` (`type="earn"`, `source="shopify"`,
   `amount_usd` == fixture's `total_price`, `points` == floor(amount ×
   `points_per_dollar`)), and the resolved member's `points_balance`
   increases by exactly that many points.
2. If no `Member` matches the fixture's `customer.email` for that merchant,
   a new `Member` is auto-created from the payload before being credited.
3. POSTing the **same** fixture (same Shopify `id`) a second time returns
   `200 {"status":"duplicate_ignored"}`, creates **no** second
   `Transaction`, and the member's balance is unchanged from step 1.
4. Missing `X-Shopify-Hmac-Sha256` header → `401`, no `Transaction` created.
5. Present but incorrect HMAC (e.g. signed with the wrong secret) → `401`.
6. Unknown `merchant_id` in the URL path → `404`.
7. Malformed JSON / payload missing `customer` entirely → `422`, no partial
   state written (no orphan `Member` or `Transaction`).
8. A request with **no** `Authorization` header but a **valid** HMAC still
   succeeds (`201`) — confirms this endpoint is intentionally outside the
   `get_current_merchant` JWT-protected group, and a request with a valid
   JWT but *no*/*bad* HMAC still gets `401` (HMAC is the only accepted
   credential here, JWT is irrelevant to this endpoint).
9. `python scripts/send_sample_shopify_webhook.py` run against a locally
   running `uvicorn` instance (seeded demo merchant) succeeds end-to-end
   with exit code 0 and prints a created transaction id + updated balance —
   this is the tester's primary "prove it works without a real Shopify
   account" check.
10. New test file `backend/tests/test_shopify_webhook.py` covers items 1–8
    via `TestClient` + the shared fixture JSON (mirrors the rigor of the
    existing `test_redemption_concurrency.py`/`test_transactions.py` style:
    exact status codes, exact balance deltas, not just "some 2xx").

### Risks / notes
- Webhook endpoints reading raw body before JSON parsing is a different
  code shape than every other endpoint in this repo (`transactions.py`,
  `members.py`, etc. all take a typed Pydantic body directly). This is
  necessary for HMAC verification (Shopify signs the *raw* bytes) — flagged
  so the coder doesn't "simplify" it back to a typed body param, which would
  silently break signature verification (re-serialized JSON does not
  byte-for-byte match what Shopify signed).
- Per-merchant secret (stored on `Merchant`, set by the seed script for the
  demo merchant) rather than a single global secret — this is the
  production-correct shape (different merchants would have different
  Shopify apps/secrets) and costs nothing extra to implement now.
- Does not implement Shopify webhook *registration* (there is no real
  Shopify store) — explicitly out of scope, matching the "mocked/sandboxed"
  framing in the task.

---

## Feature 3 — Multi-user merchant accounts with role-based access

### Design
Introduce a new `TeamMember` model (**not** named `User` or `Member` —
`Member` already means "end loyalty-program shopper" in this codebase,
reusing that name would be actively confusing) belonging to a `Merchant`,
with `role` ∈ {`admin`, `member`}. `Merchant` becomes a pure business-entity
record; login credentials (`email`, `hashed_password`) move from `Merchant`
onto `TeamMember`. JWTs now carry the team member's id (`sub`), plus new
`merchant_id` and `role` claims. To minimize blast radius across the
existing routers, `get_current_merchant` (used today by `members.py`,
`transactions.py`, `rewards.py`, `ai.py`) is **kept as a thin wrapper**
around a new `get_current_user` dependency, so none of those four router
files need to change.

### New/changed files

**`backend/app/db/models.py`**
- Add `class TeamRole(str, enum.Enum): ADMIN = "admin"; MEMBER = "member"`.
- Add `TeamMember` model: `id`, `merchant_id` (FK, indexed), `email`
  (String(255), **unique=True globally** — same constraint shape
  `Merchant.email` had, so login-by-email-only logic barely changes),
  `hashed_password`, `role` (String(20), default `"member"`), `is_active`
  (Boolean, default True), `created_at`. Relationship
  `merchant: Mapped["Merchant"] = relationship(back_populates="team_members")`.
- `Merchant`: **remove** `email` and `hashed_password` columns (see
  "Breaking changes" below — this is safe under the clean-cutover
  assumption from Feature 1). Add
  `team_members: Mapped[list["TeamMember"]] = relationship(back_populates="merchant", cascade="all, delete-orphan")`.

**`backend/app/schemas/auth.py`**
- `MerchantSignup` unchanged (still `business_name`, `email`, `password` —
  now creates a `Merchant` + first `TeamMember(role="admin")` under the
  hood, response shape unchanged).
- New `TeamMemberCreate` (`email`, `password`, `role: Literal["admin","member"] = "member"`).
- New `TeamMemberOut` (`id`, `email`, `role`, `is_active`, `created_at` —
  **never** `hashed_password`).
- New `RoleUpdate` (`role: Literal["admin","member"]`).
- Replace `MerchantOut` with `MeOut` (`id` [team member id], `merchant_id`,
  `business_name`, `email`, `role`) — **keeps top-level `business_name` and
  `email` fields** so the existing frontend (`Layout.tsx` reads
  `merchant.business_name`/`merchant.email`, verified by grep — no other
  frontend file reads `.id` off this object) keeps working with zero
  frontend changes for the login/display path. (Frontend team-management
  UI itself — inviting/listing/removing members — is out of scope for this
  batch; flagged as a natural Batch 2 frontend follow-up.)

**`backend/app/services/security.py`**
- `create_access_token` call sites updated to pass
  `extra_claims={"merchant_id": team_member.merchant_id, "role": team_member.role}`
  — function signature itself is unchanged.

**`backend/app/api/auth.py`**
- `signup`: create `Merchant(business_name=...)` (no email/password), then
  `TeamMember(merchant_id=merchant.id, email=payload.email, hashed_password=hash_password(payload.password), role="admin")`
  in the same transaction. Response still `MeOut`-shaped, `201`.
- `login`: query `TeamMember` by `email` (was `Merchant` by `email`),
  `verify_password` against `TeamMember.hashed_password`, reject inactive
  team members with `401` too. Token subject is now `team_member.id`.
- `me`: returns `MeOut` built from `current_user` + `current_user.merchant`.

**`backend/app/api/deps.py`**
- New `get_current_user(token, db) -> TeamMember`: decodes JWT, loads
  `TeamMember` by `sub`, 401 if missing/inactive (same exception shape as
  today).
- `get_current_merchant(current_user: TeamMember = Depends(get_current_user)) -> Merchant: return current_user.merchant`
  — **unchanged signature/behavior from the caller's perspective**, so
  `members.py`, `transactions.py`, `rewards.py`, `ai.py` require **zero**
  changes.
- New `require_admin(current_user: TeamMember = Depends(get_current_user)) -> TeamMember`:
  raises `403` if `current_user.role != "admin"`.

**`backend/app/api/team.py` (new router, prefix `/api/v1/team`)**
- `GET /api/v1/team` (any authenticated team member) — list team members
  of `current_user.merchant_id`.
- `POST /api/v1/team/invite` (admin only, `require_admin`) — body
  `TeamMemberCreate`; 400 if email already registered (any merchant, since
  email is globally unique); creates the new `TeamMember` under the
  caller's merchant. **Assumption:** no real email-delivery/invite-token
  flow exists in this MVP (no email service is wired up anywhere in the
  app) — "invite" means an admin directly sets the new teammate's initial
  email+password, same pragmatic shape as the existing `/auth/signup`
  dev-convenience endpoint. Real email-based invites are explicitly
  deferred.
- `DELETE /api/v1/team/{team_member_id}` (admin only) — 404 if not found or
  belongs to a different merchant (no cross-merchant IDOR); **409** if this
  would remove the merchant's last remaining active admin (lockout guard).
- `PATCH /api/v1/team/{team_member_id}/role` (admin only) — body
  `RoleUpdate`; same 404/cross-merchant scoping; same 409 lockout guard
  when demoting the sole remaining admin.

**`backend/scripts/seed_data.py`**
- Replace the direct `Merchant(email=..., hashed_password=...)` construction
  with `Merchant(business_name=...)` + a `TeamMember` using
  `DEMO_MERCHANT_EMAIL`/`DEMO_MERCHANT_PASSWORD` and `role="admin"` — this
  is the "existing demo merchant becomes the first admin user" requirement.
  Also seed one additional demo teammate,
  `demo-member@merchant.com` / `demo1234`, `role="member"`, so the tester
  has a ready-made non-admin account to exercise the role-gating acceptance
  criteria without needing the invite endpoint first. Print both sets of
  credentials in the seed script's output.

**`backend/app/main.py`**
- `app.include_router(team.router)`.

### Breaking-change impact on existing tests (must fix, concrete locations)
- `backend/tests/test_ledger.py:19` and `backend/tests/test_recommender.py:12`
  both directly construct
  `Merchant(business_name="Test Co", email="owner@test.co", hashed_password="x")`
  at the ORM level. Since `Merchant.email`/`Merchant.hashed_password` are
  being **removed**, these two lines must be updated to drop the
  `email=`/`hashed_password=` kwargs (they only need `business_name` — the
  fixtures don't test auth). **Grep confirms these are the only two
  call sites in the entire test suite that construct `Merchant(...)`
  directly with those fields** — no other test file is affected by the
  column removal.
- `backend/tests/test_auth.py`'s four tests (`test_signup_then_login_succeeds`,
  `test_login_with_wrong_password_is_rejected`,
  `test_login_with_unknown_email_is_rejected`,
  `test_protected_endpoint_requires_token`,
  `test_protected_endpoint_rejects_garbage_token`) only assert on HTTP
  status codes and top-level response fields (`access_token`, `token_type`)
  — none of them inspect JWT claims or ORM internals, so they should pass
  **unmodified** against the new `TeamMember`-backed auth flow.
- Every other test file that logs in via `demo_credentials`/`client`
  fixtures (auth flows in `test_members.py`, `test_transactions.py`, etc.)
  goes through the public `/auth/login` HTTP contract only — unaffected as
  long as the seeded demo admin keeps the same email/password (it does).
- **New explicit regression requirement for this batch:** add at least one
  test that logs in as the seeded `demo-member@merchant.com` (`role=member`)
  and confirms `/api/v1/members`, `/api/v1/transactions`, `/api/v1/rewards`,
  `/api/v1/ai/churn` all still return `200` for a member-role token (proves
  the `get_current_merchant` wrapper still works for non-admin roles, not
  just admins).

### Acceptance criteria
1. `POST /auth/signup` → `201` with the same response shape as before
   (now `MeOut`, still includes `business_name`/`email`); immediately
   logging in with those same credentials → `200` + `access_token` (existing
   `test_signup_then_login_succeeds` behavior preserved).
2. Decoded JWT (`jose.jwt.decode` with the test secret) contains `sub`
   (a `TeamMember` id, not a `Merchant` id), `merchant_id`, and `role`
   claims — new test asserts this directly.
3. `GET /api/v1/team` with the seeded admin token → `200`, includes both
   seeded team members. Same call with the seeded member token → `200`
   also (read access is allowed for both roles).
4. `POST /api/v1/team/invite` as admin → `201`, new `TeamMemberOut` with no
   `hashed_password` field anywhere in the response body. Same call with
   the member-role token → `403`.
5. `DELETE /api/v1/team/{id}` as admin removes a non-admin teammate → the
   removed teammate can no longer log in (`401` on next `/auth/login`
   attempt with their old credentials).
6. `DELETE`/`PATCH .../role` attempting to remove or demote the **sole
   remaining admin** → `409`, no change persisted (verify by re-fetching
   `GET /api/v1/team` and confirming the admin is still present/still
   admin).
7. Cross-merchant isolation: a team member from Merchant A cannot see or
   modify Merchant B's team via `GET/POST/DELETE/PATCH /api/v1/team/*`
   (404, not leaking existence) — mirrors the existing
   `test_members_are_scoped_to_merchant` pattern already in the suite.
8. All pre-existing merchant-scoped endpoints continue to return `200` for
   both the seeded admin token and the seeded member token (see regression
   requirement above).
9. Full existing suite (with the two one-line fixture edits noted above)
   passes: 42/42 plus all new tests added across Features 2 and 3.
10. README's documented demo login (`demo@merchant.com` / `demo1234`) still
    works exactly as documented, and the seed script's printed output now
    also shows the demo member account.

### Risks / notes
- **JWT shape change is the one behavior change with real blast radius.**
  `sub` used to be a `Merchant` id; it is now a `TeamMember` id. Nothing
  outside this repo consumes these tokens yet (no external API clients, no
  mobile app), so this is safe to change now — but it must not be treated
  as backward-compatible; any previously-issued token becomes invalid the
  moment this ships (acceptable — this is a pre-launch demo app, sessions
  are 12h and everyone just logs in again).
- **Schema change (dropping `Merchant.email`/`hashed_password`) only works
  cleanly paired with Feature 1's clean-cutover/`create_all` approach.**
  Anyone still running the *old* SQLite file locally must delete
  `loyalty.db` and re-run the seed script rather than expecting an
  in-place, non-destructive upgrade — call this out in the README migration
  note for this batch.
- `/auth/me`'s `id` field now means "team member id," not "merchant id" —
  documented above; confirmed via grep that the frontend never reads
  `.id` off this object today, so this is a safe rename, not a silent
  frontend break — but the coder should still say so explicitly in their
  own PR notes in case a later batch adds a frontend consumer that assumes
  `id == merchant id`.
- Frontend team-management UI (invite/list/remove screens) is **not**
  included in this batch — backend-only, matching the task's framing
  ("plan the auth changes... the new endpoints"). Flag as expected Batch 2+
  scope so the tester doesn't fail this batch for missing UI.

---

## Cross-cutting summary: what could break the existing 42 tests

| Change | Existing test(s) at risk | Required fix |
|---|---|---|
| `Merchant.email`/`hashed_password` columns removed (Feature 3) | `test_ledger.py:19`, `test_recommender.py:12` (direct `Merchant(...)` construction) | Drop the two now-invalid kwargs from those fixtures |
| JWT `sub`/claims change (Feature 3) | None inspect claims directly today — but any *future* code that assumed `sub == merchant.id` must not be reintroduced | New test explicitly asserts new claim shape |
| `DATABASE_URL` handling (Feature 1) | None — `conftest.py` already force-overrides `DATABASE_URL` to SQLite before any app import | No change needed to test infra |
| New nullable columns (`external_order_id`, `source`, `shopify_*`) (Feature 2) | None — additive, defaulted, nullable | None |
| Dockerfile CMD no longer unconditionally reseeds (Feature 1) | None (not exercised by pytest) | Manual/infra-stage verification only (see Feature 1 acceptance #4) |

**Net:** with the two one-line fixture edits called out above, all 42
existing tests are expected to keep passing unmodified, plus this batch
adds new tests for: DB URL normalization (1), Shopify webhook (≈8), team
roles/JWT claims (≈8+). The tester should treat "42 baseline tests still
green" and "new tests for all three features per the acceptance criteria
above" as two separate, both-required bars.
