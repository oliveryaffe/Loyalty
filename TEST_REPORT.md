# TEST_REPORT.md — Loyalty AI Framework (Tester Stage)

Verification performed independently against the coder's deliverable, per
PLAN.md §6. All commands were re-run from scratch (fresh venv, fresh npm
install, fresh seed) rather than trusting the coder's self-report.

---

## (a) Independent verification of coder's claims

| Claim | Verified? | Evidence |
|---|---|---|
| "39/39 backend pytest tests pass" | **CONFIRMED** | Fresh `python3 -m venv`, `pip install -r requirements.txt` (31s, no errors), `pytest -v` → `39 passed, 9 warnings in 17.36s`. Same 39 test IDs as claimed. |
| "Clean frontend TypeScript build" | **CONFIRMED** | Fresh `npm install` (72 packages, 4s) + `npm run build` (`tsc -b && vite build`) → 0 TypeScript errors, `dist/` produced in 1.3s. Coder's README note about `ENOTEMPTY` on the network mount did **not** reproduce for me copying to `/tmp` first; build is genuinely clean. |
| "Seed script populates 620 members, ~7,200 txns, fraud-like patterns" | **CONFIRMED** | Re-ran seed: `Created 620 members. Created 7212 transactions (128 intentionally fraud-like).` |
| "Fraud detector catches injected fraud, FPR < 5%" | **CONFIRMED, and better than claimed** | Computed directly against fresh seed: **recall = 96.09%** (123/128 injected fraud txns flagged), **false-positive rate = 0.01%** (1/7273 normal txns flagged). |
| "Churn model separates lapsing vs loyal" | **CONFIRMED, strongly** | Computed directly: loyal avg risk = **5.3**, average = 30.5, at_risk = 66.9, lapsing = **97.0** (0–100 scale). Far exceeds the test's own 25-point-gap bar. |

The 39-passing-tests and clean-build claims are both true. However, the
**backend has a critical, unverified-by-the-test-suite race condition** in
the redemption path — see §(c). The existing tests only exercise
sequential/single-threaded behavior, so this bug was invisible to the
coder's own test run.

---

## (b) PLAN.md §6 acceptance checklist

**Functional**
- [x] Backend runs locally via `uvicorn` with a single documented command — confirmed, `/health` returns 200.
- [x] Seed script populates members/transactions/rewards + injected fraud — confirmed (620/7212/128 above).
- [x] Merchant login works; invalid credentials rejected — confirmed (401 on wrong password, 401 on unknown email, 422 on missing fields).
- [x] Members list loads real seeded data incl. churn-risk column — confirmed via API and code (`MemberWithChurn` includes `churn_risk_score`/`churn_risk_band`).
- [x] Member earns points via transaction-ingestion; balance updates correctly — confirmed in the single-request case. **PARTIAL under concurrency — see critical bug below.**
- [x] Redeem affordable reward succeeds; insufficient balance rejected (single request) — confirmed (400 + balance unchanged). **FAILS under concurrent requests — see critical bug below.**
- [x] `/ai/recommendations/{member_id}` returns non-empty ranked list — confirmed via unit tests and live curl against seeded members with history.
- [x] `/ai/churn` differentiates lapsing vs active cohorts — confirmed, strongly (97.0 vs 5.3 avg).
- [x] `/ai/fraud-alerts` flags injected fraud, FPR stays low — confirmed (96% recall, 0.01% FPR).
- [x] Fraud alerts visible in dashboard — `FraudAlerts.tsx` calls `getFraudAlerts()`; not manually clicked through in a browser (no browser tooling used in this pass) but the API contract and component wiring are correct by code inspection.

**Non-functional / quality**
- [x] Backend unit tests pass, covering ledger math + all 3 AI modules — confirmed, and tests are substantive (see §d).
- [x] No secrets committed; config via env vars — confirmed. Grepped for hardcoded secrets/passwords/keys; only match is the documented demo password (`demo1234`, intentionally public per README). `JWT_SECRET_KEY` has an insecure *dev default* (`dev-only-insecure-secret-change-me`) but this is explicitly flagged in README/config.py comments as needing override in shared/deployed environments — acceptable for MVP.
- [x] README allows fresh install/seed/run end-to-end without undocumented steps — confirmed; followed it verbatim and it worked (module for module) except the frontend `ENOTEMPTY` caveat the README itself already documents as sandbox-specific.

All explicitly-out-of-scope items (multi-tenant billing, real POS integration, SSO/OAuth, dynamic reward pricing) were correctly not attempted and are not counted against the build.

---

## (c) Bugs / issues found

### CRITICAL — Redemption race condition allows unlimited overspending of points
**Repro:** Create a member with exactly 100 points. Create a reward costing
100 points. Fire 10 concurrent `POST /api/v1/rewards/redeem` requests for
that member/reward pair.
**Result:** All 10 requests returned `HTTP 200` with `"status":"completed"`,
each recording a distinct `Redemption` row (`status=completed,
points_spent=100`) and a distinct `-100` ledger `Transaction` row — i.e.
the system recorded **10 completed, valid-looking redemptions (1,000
points) against a member who only ever had 100**. The cached
`member.points_balance` field ended at `0` (not `-900`), because the
balance check-then-decrement in `redeem_reward()` (`backend/app/services/ledger.py`
lines 120–132) is not protected by any row lock, `SELECT ... FOR UPDATE`,
atomic `UPDATE ... WHERE balance >= cost`, or serializable transaction —
it's a classic TOCTOU/lost-update race. The `Redemption` table (the system
of record a merchant would use to decide what to fulfill) is now
**inconsistent with the actual point balance**: it says 10 rewards were
legitimately earned and redeemed when only 1 could have been. In a real
deployment this is a direct path to inventory/financial loss — a member
(or scripted attacker) can get N free rewards for the price of 1 simply by
double-clicking or firing a small burst of requests, something any
real-world client (including a flaky mobile connection retrying a POST)
can trigger by accident. This is exactly the kind of fraud vector the
product's own fraud detector is supposed to guard against, and it exists
in the core ledger itself. **Not covered by any existing test** — all 39
tests are single-request/sequential.
**Fix direction:** wrap the balance check + decrement in a single atomic
SQL statement (`UPDATE members SET points_balance = points_balance - :cost
WHERE id = :id AND points_balance >= :cost`, check rowcount) or use
`SELECT ... FOR UPDATE` inside a DB transaction before checking balance.

### MAJOR — No upper bound on transaction amount
**Repro:** `POST /api/v1/transactions {"member_id": "...", "amount_usd":
99999999999}` → `HTTP 201`, credits 99,999,999,999 points to the member.
`TransactionCreate.amount_usd` (`backend/app/schemas/transaction.py`) only
enforces `gt=0`, no upper bound. A single fat-fingered or malicious
ingestion call can mint an arbitrary number of points. Low likelihood in
an MVP context (no external POS integration yet, per PLAN.md A4) but this
is exactly the kind of validation gap that becomes a real incident the
moment a POS webhook integration is added.

### MINOR — Malformed numeric input (`NaN`) crashes with an unhandled 500
**Repro:** `POST /api/v1/transactions {"member_id": "...", "amount_usd":
NaN}` → `HTTP 500 Internal Server Error` (generic body, no stack trace
leaked to the client — good — but server log shows an unhandled
`ValueError: Out of range float values are not JSON compliant` inside
FastAPI's *own* validation-error serializer, because Pydantic accepts
`NaN` as a valid float satisfying `gt=0` is False... actually it fails the
`gt=0` check as expected, but then FastAPI tries to JSON-serialize the
422 error body which echoes back the invalid `NaN` input value, and
Python's `json.dumps` refuses to encode `NaN` by default, throwing inside
the exception handler itself). Should return a clean 422. Does not leak
secrets/internals to the client, so this does not violate the "no 500s
leaking internals" bar in a security sense, but it's a genuine crash path
on adversarial input and worth fixing (e.g. reject non-finite floats
explicitly in the schema, or configure the JSON encoder).

### MINOR — No idempotency protection on transaction ingestion
**Repro:** POST the identical transaction payload twice → both are
recorded as separate earn events, balance double-credited. Not a stated
PLAN.md/acceptance requirement, and arguably reasonable for MVP scope
(A4: no real POS integration yet), but worth flagging since real POS/
webhook systems routinely redeliver events, and there's currently nothing
(idempotency key, dedup window) to prevent double-crediting on retry.

### MINOR — No field length limits on member fields
**Repro:** `POST /api/v1/members` with a 5MB `first_name` string →
`HTTP 201`, accepted and stored. No max-length constraint in
`MemberCreate`. Low risk at MVP scale but an easy DoS-via-storage-bloat
vector and would bloat the `Members` list response.

### MINOR — Cosmetic: seed script prints a scary-looking (harmless) traceback
Running `python3 scripts/seed_data.py` prints `(trapped) error reading
bcrypt version` plus a `passlib`/`bcrypt` version-compatibility traceback
to stderr before continuing normally. It's non-fatal (seed completes
successfully) but looks alarming to a first-time operator following the
README and could be mistaken for a real failure. Caused by a
passlib/bcrypt version mismatch in `requirements.txt` (`bcrypt>=3.2,<5`
pinned range still hits this on the resolved version); worth pinning more
tightly or suppressing.

### MINOR — Frontend npm audit flags 4 known vulnerabilities (3 moderate, 1 high)
Transitive: `esbuild`/`vite` dev-server request-forwarding issue and a
`react-router` open-redirect CVE. Both are dev-dependency/known-CVE noise
typical of any current React+Vite project, not something the coder
introduced through negligence, and not exploitable in the way this MVP is
used (no production deployment yet). Flagging for awareness, not blocking.

### Notes, not bugs
- JWT is stored in `localStorage` on the frontend (`frontend/src/api/client.ts`), which is XSS-exposed by nature. Standard for MVP-stage SPAs and PLAN.md explicitly scopes production-grade auth as future work — noting, not failing.
- All list/detail endpoints correctly scope by `merchant.id` (verified in `members.py`, `transactions.py`, `rewards.py`, `ai.py` and via `test_members_are_scoped_to_merchant`) — no cross-tenant IDOR found.
- Auth, malformed-JWT, SQLi-style-string, empty-body, wrong-content-type, and broken-JSON-syntax attacks all returned clean, appropriate 4xx responses with no internal detail leakage.

---

## (d) Test quality assessment

The existing 39 tests are **genuinely good, not tautological**. Specific
evidence:

- **Ledger tests** (`test_ledger.py`) assert actual arithmetic (`floor`,
  not round; accumulation across multiple transactions; exact
  post-redemption balances), not just status codes.
- **Fraud tests** (`test_fraud_detector.py`) include a real
  precision/recall-style check
  (`test_run_fraud_detection_against_seeded_data_recall_and_false_positive_rate`)
  computed against the seed script's ground-truth `synthetic_fraud_label`,
  asserting `recall >= 0.7` and `false_positive_rate < 5%` — this is
  exactly the kind of statistically meaningful test the task asked me to
  look for, and it was already there. I independently reproduced these
  numbers outside pytest (96.09% recall, 0.01% FPR) and got a materially
  *better* result than the test's own threshold, confirming it isn't
  a lucky/brittle assertion.
- **Churn tests** assert a concrete 25-point gap between lapsing/loyal
  cohort averages and that ≥80% of each cohort lands in the expected risk
  band — not just "score exists."
- **Recommender tests** assert relative ranking (affordable > unaffordable,
  category-affinity boosts the matching reward) with real score
  comparisons.
- **API/auth tests** check exact status codes tied to specific negative
  scenarios (wrong password → 401, missing token → 401, garbage token →
  401, tier-ineligible → the ledger correctly raises) rather than only
  happy-path 200s.

The one real gap: **no concurrency/race-condition tests exist anywhere in
the suite**, which is precisely how the critical redemption bug above
went undetected. All fixtures use a single sequential `TestClient` call
pattern. I'd flag this as the main test-suite blind spot going forward.

Frontend has **zero automated tests** (`frontend/tests/` is empty, RTL was
never wired up) — openly disclosed by the coder in README §6 as a
deviation. I did not add frontend tests myself (out of scope for
verification), but this remains a real gap: the dashboard's data-wiring
correctness (e.g., does the churn badge actually render the right band
color, does the reward redemption button correctly disable when
unaffordable) is unverified by anything but manual/code inspection.

---

## (e) Overall verdict

**Not yet acceptance-ready — one critical fix required before sign-off.**

Everything the coder claimed about test pass rate, build cleanliness, and
AI-layer quality is **true and independently reproduced** — this is a
solid MVP implementation with genuinely substantive tests and AI modules
that measurably work (96% fraud recall, clear churn cohort separation).
CORS, tenant isolation, input validation on the happy/most adversarial
paths, and error-response hygiene are all in good shape.

**What's blocking:** the redemption race condition (§c, CRITICAL) is a
real, easily-reproduced financial-integrity bug in the core loyalty
ledger — the one subsystem this entire product exists to get right. It
was invisible to the existing test suite because nothing there tests
concurrent access, and it directly undermines the "ledger math
correctness" acceptance bar (redemption is only correctly rejected when
requests are sequential; concurrent requests can redeem far more than a
member's balance permits, with the audit trail falsely showing all of
them as validly completed). This should go back to the coder stage with:
1. an atomic balance-check-and-debit fix in
   `backend/app/services/ledger.py::redeem_reward`, and
2. at least one new test that fires concurrent redemption requests against
   an exact-balance boundary and asserts only one succeeds.

The unbounded-transaction-amount issue (MAJOR) and the NaN-input 500
(MINOR) are worth fixing in the same pass but are not, by themselves,
blocking in the way the race condition is. Everything else in this report
is minor/cosmetic and does not block MVP acceptance per PLAN.md §6's
scope.
