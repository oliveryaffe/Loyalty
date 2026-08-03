# TEST_REPORT_BATCH1.md — Ledgerly Batch 1 (Postgres/Bootstrap, Shopify Webhooks, Multi-user Roles)

Independent adversarial verification against `PLAN_BATCH1.md`. This is a fresh redo (not a resume of a prior cut-off run) — every claim below was independently reproduced in this pass. Status: COMPLETE.

> **Update (post-report coder pass):** both CRITICAL-1 and CRITICAL-2 below have since been fixed. See the "Two critical bugs from TEST_REPORT_BATCH1.md" section in `README.md` §6 for what changed, the new regression tests in `backend/tests/test_earn_concurrency.py`, and re-verification evidence (seed-twice repro re-run, tester's forced-interleaving repro re-run, full 69/69 suite passing). This report's findings below are left unedited as the original adversarial record.

---

## (a) Independent verification of coder's headline claims

| Claim | Verified? | Evidence |
|---|---|---|
| "67/67 tests passing (42 original + 25 new)" | **CONFIRMED** | Fresh `python3 -m venv`, `pip install -q -r requirements.txt` (clean install, no build errors, `psycopg2-binary` wheel installed with no `libpq-dev` needed), `python3 -m pytest -q` → `67 passed, 10 warnings in 30.81s`. |
| "`--seed-if-empty` makes redeploys safe" (Feature 1, the actual bug fix) | **FALSE — NOT WIRED IN** | `backend/Dockerfile` line 12 is still `CMD ["sh", "-c", "python scripts/seed_data.py && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]` — **no `--seed-if-empty` flag present**. `seed_if_empty()` itself is implemented correctly in `scripts/seed_data.py` (verified in isolation, see §(c)), but the one-line Dockerfile change PLAN_BATCH1.md calls "the single highest-risk item in Feature 1" / "must fix" was never made. See CRITICAL-1 below. |
| "Postgres DSN normalization works" | **CONFIRMED, with minor gaps** | `test_config.py`'s 5 tests pass; independently re-verified with additional edge cases (query params, `postgresql+asyncpg://` passthrough, empty string, malformed non-URL string). See §(c) MINOR-1. |
| "Shopify webhook ingestion is idempotent under redelivery" | **PARTIALLY FALSE — concurrency race found** | Sequential idempotency (coder's own tests, and manually replaying the fixture twice via TestClient) works correctly. Under genuine concurrency, the idempotency check is a TOCTOU race — see CRITICAL-2 below. |
| "Multi-user roles / admin gating work correctly" | **CONFIRMED** | `test_team.py`'s 12 tests pass; independently re-verified member self-escalation attempts, JWT tampering (wrong signing key, `alg=none`), cross-tenant team access, last-admin lockout (delete + demote paths), demotion-takes-effect-immediately. No bugs found in this subsystem — see §(b) items 4-8. |

---

## (b) PLAN_BATCH1.md acceptance checklist — item by item

### Feature 1 — Postgres + bootstrap
1. `uvicorn` starts against Postgres, `GET /health` → 200 — **NOT INDEPENDENTLY TESTED** (no live Postgres instance available in this sandbox; code path (`app/main.py` health route, `pool_pre_ping=True` in `app/db/base.py`) looks correct by inspection). **PARTIAL / UNVERIFIED**.
2. DSN normalization unit test — **PASS**. `test_config.py` covers `postgres://`, `postgresql://`, already-qualified, sqlite file, sqlite memory. Independently re-verified additional cases (see §(d)/§(c)).
3. `seed_data.py` against real Postgres produces same counts as SQLite — **NOT TESTED** (no Postgres instance available; flagged for the infra stage to verify against the real Railway Postgres).
4. Restart-durability (the actual bug) — **FAIL**. Not because the mechanism doesn't work in isolation, but because it is never invoked: the Dockerfile still calls `seed_data.py` with no flag, i.e. `reset=True` (see `seed()` default and `if __name__ == "__main__"` block — `args.seed_if_empty` defaults `False`, falls to `seed(reset=not args.no_reset)` = `seed(reset=True)`). Reproduced empirically, see CRITICAL-1.
5. Fresh/empty Postgres still auto-seeds on first boot — **CONFIRMED IN ISOLATION** (`seed_if_empty()` logic correct, see §(e)) but moot for the real deploy artifact until Dockerfile is fixed.
6. All 42 existing tests still pass — **PASS** (67 total, includes the 42).
7. `pip install -r requirements.txt` succeeds, no build errors — **PASS**, confirmed fresh venv install above.

### Feature 2 — Shopify webhook
1. Valid HMAC → 201, correct Transaction/points/balance — **PASS**, reproduced live (see §(c) live-server testing).
2. Auto-create Member if no email match — **PASS**, reproduced live.
3. Same fixture replayed → 200 duplicate_ignored, no 2nd Transaction, balance unchanged — **PASS sequentially**, **FAILS under concurrency** — CRITICAL-2.
4. Missing HMAC header → 401 — **PASS**, reproduced live.
5. Incorrect HMAC (wrong secret) → 401 — **PASS**, reproduced live, including the specific cross-merchant-secret-confusion case (see §(c)).
6. Unknown merchant_id → 404 — **PASS**, reproduced live.
7. Malformed JSON / missing customer → 422, no partial state — **PASS**, reproduced live.
8. No Authorization header + valid HMAC → 201; valid JWT + bad/missing HMAC → 401 — **PASS**, reproduced live.
9. `send_sample_shopify_webhook.py` end-to-end against live uvicorn → exit 0 — **PASS** (see §(c)).
10. `test_shopify_webhook.py` covers items 1-8 — **PASS, substantive** (see §(d)), but does **not** cover concurrent replay (CRITICAL-2) or cross-merchant secret confusion, or oversized payload / wrong Content-Type — all of which this pass added independently.

### Feature 3 — Multi-user roles
1. Signup → 201 MeOut shape, login works — **PASS**.
2. JWT contains sub/merchant_id/role — **PASS**, `test_jwt_contains_sub_merchant_id_and_role_claims` + independently decoded a live token.
3. `GET /team` works for both admin and member tokens — **PASS**.
4. Invite as admin → 201 no password leak; as member → 403 — **PASS**, and independently confirmed a member cannot self-escalate via any endpoint (see §(c)).
5. Delete non-admin teammate → they can no longer log in — **PASS**.
6. Delete/demote sole admin → 409, no change persisted — **PASS**, both paths.
7. Cross-merchant isolation on `/team/*` → 404 not leaked — **PASS**.
8. Pre-existing endpoints (members/transactions/rewards/ai) return 200 for both admin and member tokens — **PASS**.
9. Full suite (42 + new) passes — **PASS** (67/67).
10. README demo login still documented/works, seed script prints member account too — **PASS** by inspection of `seed_data.py` output block.

---

## (c) Bugs / issues found (adversarial testing beyond the coder's own checks)

### CRITICAL-1 — Dockerfile never wires up `--seed-if-empty`; the actual persistence bug is NOT fixed
**File:** `backend/Dockerfile` line 12.
Still reads:
```
CMD ["sh", "-c", "python scripts/seed_data.py && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
```
No `--seed-if-empty`. `seed_data.py`'s `__main__` block defaults to `seed(reset=not args.no_reset)` i.e. `reset=True` — `Base.metadata.drop_all()` then `create_all()` — every single container start (including a plain restart/redeploy with no code change) **wipes the entire database**, which is exactly the bug PLAN_BATCH1.md's Feature 1 exists to fix, and which the plan explicitly flags as "the single highest-risk item in Feature 1" / "must fix."

**Repro:**
```
cd backend
python scripts/seed_data.py          # boot #1 (what Dockerfile currently does)
sqlite3 loyalty.db "select id from merchants limit 1;"   # note merchant id A
python scripts/seed_data.py          # boot #2, simulating a restart, still what Dockerfile does
sqlite3 loyalty.db "select id from merchants limit 1;"   # merchant id is now B != A, all data regenerated
```
Running the *intended* command instead (`python scripts/seed_data.py --seed-if-empty` twice) correctly no-ops on the second run and preserves the original merchant id / data — proving the fix exists in `scripts/seed_data.py` but was never connected to the deployment entrypoint. **Every claim in the coder's self-report and in this batch's Feature 1 acceptance criteria #4/#5 about restart-durability is false for the actual shipped artifact.**

**Severity:** Critical — this is the one thing Feature 1 was for, and it silently doesn't work; nothing in the test suite catches it because pytest never exercises the Dockerfile (correctly noted as a gap in PLAN_BATCH1.md's own cross-cutting table — "Dockerfile CMD... Manual/infra-stage verification only").

**Fix:** one-line Dockerfile change to `python scripts/seed_data.py --seed-if-empty && uvicorn ...`.

---

### CRITICAL-2 — Shopify webhook idempotency is a TOCTOU race under concurrent redelivery (same shape as the original redemption bug), PLUS the reused `earn_points()` has an independent lost-update race on `points_balance`
**Files:** `backend/app/services/shopify.py` (`ingest_shopify_order()`), `backend/app/services/ledger.py` (`earn_points()`).

The idempotency check is check-then-act with no locking and no unique DB constraint:
```python
existing = db.query(Transaction).join(Member, ...).filter(
    Member.merchant_id == merchant.id,
    Transaction.external_order_id == external_order_id,
).first()
if existing is not None:
    return None, True
# ... member resolve/create ...
txn = earn_points(db, member, amount_usd, channel="shopify")
txn.external_order_id = external_order_id
```
`Transaction.external_order_id` (`app/db/models.py`) is declared `index=True` but **not** `unique=True`. Nothing at the DB layer prevents two concurrent requests from both passing the `existing is None` check before either commits. This is the identical bug shape to the redemption race documented and fixed in the original `TEST_REPORT.md` / `test_redemption_concurrency.py` — but that fix (an atomic conditional `UPDATE`, per `app/services/ledger.py`'s own comments on `redeem_reward`) was **not** applied to this new code path, nor to `earn_points()` itself, which still does plain Python read-modify-write: `member.points_balance += points`.

**Two-track repro, both performed against real running server(s) / real DB sessions, not the shared-session TestClient:**

1. **Realistic HTTP-timing test (honest negative result on its own):** started a real `uvicorn` process — first with a single worker (in-process background thread, `ThreadPoolExecutor` firing 15 concurrent identical webhook POSTs, same technique as `test_redemption_concurrency.py`), then again with **`uvicorn --workers 3`** (genuine separate OS processes, no shared event loop) and 20 concurrent identical requests. **In both cases, only 1 of N requests returned `201`; the rest correctly returned `200 duplicate_ignored`, and exactly 1 `Transaction` row was created.** At real HTTP timing granularity (each request pays TCP + HMAC compute + JSON parse + ORM query round-trip overhead), the SELECT-to-INSERT window is apparently too narrow to hit reliably this way, even with true OS-level multi-process concurrency. **This means the bug is real but not trivially reproducible via a simple concurrent-curl-loop the way the original redemption bug was** — worth flagging honestly rather than overclaiming "confirmed via curl."
2. **Deterministic forced-interleaving test (proves the race is real in the code, independent of timing luck):** wrote a script that opens two separate DB sessions/threads, has both execute the exact same "does a Transaction with this `external_order_id` already exist?" SELECT that `ingest_shopify_order` runs, synchronizes them on a `threading.Barrier` so **both** threads are guaranteed to see `existing = None` before **either** proceeds, then lets both continue to `earn_points()` + insert + commit, replicating the real function's logic line-for-line against the real DB. **Result: both threads reported `created`, and the DB ended up with 2 `Transaction` rows sharing `external_order_id="55500001"`** — direct proof the check-then-act sequence, as written, is not protected by any DB-level constraint or lock. **Additionally, and worse: the member's final `points_balance` was `49`, not the `98` that 2 legitimate earn events would produce** — i.e. on top of the duplicate-row bug, one of the two `points_balance` increments was silently lost (classic read-modify-write lost update in `earn_points()`, the same class of bug `redeem_reward()` was specifically hardened against with an atomic `UPDATE ... WHERE points_balance >= cost` — see the code comment there explaining exactly why that pattern was chosen). This means under concurrent redelivery, the ledger can end up in a state where `Transaction` rows exist that the member's `points_balance` doesn't actually reflect — a data-integrity problem beyond just "double credited."

**Important scoping note:** `earn_points()`'s lost-update issue is **pre-existing**, not introduced by this batch — `POST /api/v1/transactions` (`app/api/transactions.py`) has called the same unprotected `earn_points()` since before Batch 1, and the original `TEST_REPORT.md` only exercised/found the *redemption* race, not the *earn* race, so this was already a latent gap. Batch 1 doesn't introduce it, but it does introduce the first genuinely concurrent, unauthenticated, retry-prone caller of it (a real payment processor's webhook system *will* retry on timeout/5xx, unlike a human clicking "add points" twice) — so this batch is what makes the pre-existing gap newly relevant/exploitable in practice, and the *new* idempotency-check race compounds it.

**Severity:** Critical (duplicate ledger rows + lost balance updates are a financial-integrity bug touching real money-equivalent points), but noted as harder to trigger via naive concurrent-request timing than the original redemption bug — practical exploitability depends on true near-simultaneous webhook redelivery (e.g. Shopify's own retry landing within microseconds of a prior in-flight delivery, or multiple app replicas behind a load balancer each processing a redelivery at once), which is plausible in production but not as trivially demonstrable as the original bug was with a bare `ThreadPoolExecutor`.

**Fix:** (a) add a DB-level `UNIQUE` constraint on `external_order_id` (scoped appropriately) and catch the resulting `IntegrityError` as the actual source of truth for duplicate detection instead of the prior SELECT; (b) separately, harden `earn_points()` with the same atomic-conditional-`UPDATE` pattern already used in `redeem_reward()` (e.g. `UPDATE members SET points_balance = points_balance + :pts WHERE id = :id`) instead of `member.points_balance += points`.

---

### VERIFIED-SAFE — Cross-merchant Shopify secret confusion (this was the task's top adversarial priority; result is PASS, not a bug)
Tested directly with two real merchants signed up via the live server: computed a valid HMAC signature using **Merchant B's** `shopify_webhook_secret` (`"secret-for-merchant-B"`), then POSTed it to **Merchant A's** webhook URL (`/api/v1/webhooks/shopify/{merchant_A_id}/orders-create`).
**Result: `401`**, correctly rejected (`{"detail":"Invalid webhook signature"}`). Control case (Merchant A's own secret against Merchant A's URL) correctly returned `201`. `verify_shopify_hmac` compares against `merchant.shopify_webhook_secret` where `merchant` is looked up strictly by the URL path id (`db.get(Merchant, merchant_id)`), so the secret used for verification is always scoped to the specific merchant in the URL, never a global or most-recently-seeded value. **This is the correct, safe design — no bug found here**, despite being the area this task flagged as most likely to hide one. See §(f) for the exact script/output.

---

### MINOR-1 — DSN normalizer: no crash on edge cases, but also no validation of the *result*
Independently tested 12 cases beyond the 5 in `test_config.py` (query-param-bearing DSNs, `postgresql+asyncpg://` passthrough, empty string, non-URL garbage string, bare `postgres://` with no host/path, `mysql://` passthrough) — every case ran without raising, and the prefix-only `startswith`/`.replace(..., 1)` implementation correctly leaves query strings and non-Postgres schemes untouched (no accidental double-replacement or over-matching). **No bug** in the transform logic itself. The only real gap: the validator never checks that its *output* is actually a well-formed, connectable DSN — `Settings(database_url="postgres://")` (no host) "normalizes" to `"postgresql+psycopg2://"`, which will only fail much later (at `create_engine`/connect time) with a possibly-confusing error, not at config-load time. Low impact, not a blocker — most real misconfigurations (typo'd host, missing password) would fail the same way with or without this validator.

### MINOR-2 — Cross-tenant email collision on `/team/invite` returns a generic 400, technically confirms email existence globally
Confirmed live: Merchant A's admin attempting to invite `adminb@b.example.com` (already a TeamMember of Merchant B) gets `400 {"detail":"Email already registered"}` — same message as a same-tenant duplicate. This is explicitly the documented/intended design (`TeamMember.email` is globally unique, per PLAN_BATCH1.md's Feature 3 design section: "400 if email already registered (any merchant, since email is globally unique)"), so **not a deviation from spec**, but worth flagging as a very minor cross-tenant information leak in the strict sense: Merchant A's admin can enumerate whether a given email address has *any* account on the platform (at any merchant) by trying to invite it and reading the status code. Low severity (requires already being an authenticated admin of *some* merchant; leaks only "this email exists somewhere," not which merchant or any other data) and explicitly a known, accepted tradeoff per the plan — documenting, not blocking.

### MINOR-3 — `--seed-if-empty`'s "is the DB empty" check only looks at `Merchant`, not `TeamMember`/`Member` — confirmed to leave a genuinely broken half-seeded state if triggered
Empirically reproduced (not just reasoned about): manually created a bare `Merchant` row via direct DB access with **zero** `TeamMember`/`Member`/`Transaction` rows (simulating any out-of-band process that inserts a Merchant without going through the atomic `seed()`/`signup` paths — e.g. a future migration script, a manual hotfix, or an admin tool), then called `seed_if_empty()`. **Result:** it printed "Database already has data... no-op" and left the DB with 1 Merchant, 0 TeamMembers, 0 Members, 0 Transactions — a merchant that **nobody can ever log into** (login requires a matching `TeamMember` row) and that never gets fixed automatically. Note: this exact interleaving *cannot* happen purely from a crash mid-`seed()` today, because `seed()` wraps everything in one transaction with `except: db.rollback()` before a single final `db.commit()` — so a crash during normal seeding rolls back the Merchant row too. The gap only manifests from an out-of-band Merchant insert outside `seed()`/`signup`, which is a real but narrow precondition. **Low-to-moderate severity, not a Batch-1 blocker**, but worth a one-line fix (check `TeamMember.first()` instead of/in addition to `Merchant.first()`) since it's cheap and the "seed script never got to redeploy safely" story is exactly what this batch is about.

### MINOR-4 — No request body size cap on the (deliberately unauthenticated) Shopify webhook endpoint
`await request.body()` reads the entire request body into memory with no size limit before HMAC verification ever runs, and no size-limiting middleware exists anywhere in `app/main.py`. Sent a 5MB JSON body with a valid signature — accepted (`201`) in ~40ms with no issue, `extra="ignore"` correctly dropped the oversized padding field. Did not push further (multi-GB) given the shared sandbox environment, but the code path has no structural size cap, and this endpoint is explicitly *not* behind JWT auth (by design, since it's a webhook) — an attacker who has ever observed a valid `merchant_id` (a 32-char hex UUID, not practically guessable, but present in plaintext in every legitimate webhook URL / the demo script's output) could send arbitrarily large bodies to force memory allocation before the 401 for a bad signature is ever returned. Low severity given the merchant_id isn't guessable, but worth a `Content-Length` pre-check or ASGI-level body size limit as defense-in-depth for a genuinely public, unauthenticated endpoint. Not a Batch-1 blocker.

---

## (d) Test quality assessment

**`test_config.py`** — 5 tests, narrow but correct for what they claim: pure unit tests of the validator, all 4 documented DSN shapes plus sqlite passthrough covered. Does not test malformed/non-URL strings, empty string, or DSNs with query params — reasonable for a one-line regex-ish transform, but "5 tests" undersells that this validator is trivial; no adversarial cases. **Shallow but adequate for its narrow scope.**

**`test_shopify_webhook.py`** — 9 tests, genuinely substantive: exact status codes, exact balance deltas (not "some 2xx"), checks `Transaction` row shape/fields, checks idempotent-replay leaves balance unchanged and txn count at 1, checks no-partial-state-on-422 (both no orphan Member AND no orphan Transaction), and explicitly tests the JWT-is-irrelevant-here / HMAC-is-the-only-credential distinction (`test_valid_jwt_but_bad_hmac_still_401s`) which is an easy thing to get wrong and good that it's covered. **Gaps (all covered by this pass's independent live testing instead):** no concurrent-replay test (the actual bug, CRITICAL-2), no cross-merchant-secret test, no oversized-payload test, no wrong-Content-Type test. Good rigor within its scope, but the single most important adversarial case for a webhook endpoint — concurrent redelivery — is untested, which is exactly where the real bug was hiding.

**`test_team.py`** — 12 tests, the strongest of the three new files: covers JWT claim shape, invite/list/role-gate, both lockout paths (delete AND demote) for the sole admin, the "another admin exists, removal/demotion is fine" positive case (good — many suites only test the negative/lockout case), cross-merchant 404-not-leaked on all of GET/DELETE/PATCH, and the regression requirement (member-role token still works on all four pre-existing routers) via the real seeded DB. **Gap:** no explicit test of a member attempting a **direct self-role-escalation** payload trick (e.g. does `PATCH /team/{own_id}/role` even get reached before `require_admin` fires — it doesn't, `require_admin` runs first via `Depends`, verified by inspection and independently re-tested live, PASS) — this pass added that test live since it's an easy thing to almost-get-wrong (e.g. if role-gating were checked only in the handler body after already fetching/mutating the target). No test of inviting an email that already exists as a TeamMember on a **different** merchant (cross-tenant email collision) — added independently, see §(c)/live section — behavior is a plain `400` (global uniqueness is enforced, arguably a minor cross-tenant information leak — see MINOR-2 below, added during live testing).

**Overall test quality:** good-to-strong for team/webhook logic-level correctness, systematically weak on concurrency (the one place this codebase has a known history of bugs) and on Dockerfile/deployment-artifact verification (correctly, PLAN_BATCH1.md itself flags this as out of pytest's reach — but that then makes it entirely the tester's job, and it is in fact broken).

---

## (e) Migration / bootstrap story (`--seed-if-empty` edge cases)

Tested directly against `scripts/seed_data.py`'s `seed_if_empty()`:
- **Empty DB → seeds.** Confirmed (fresh SQLite file, `seed_if_empty()` populated 620 members etc).
- **DB with a `Merchant` row already present → true no-op.** Confirmed, does not touch/re-check `team_members`/`transactions`/etc.
- **Two-runs-in-a-row (the actual restart scenario):** ran `seed_if_empty()` twice programmatically against the same SQLite file — 1st run seeds, 2nd run is a true no-op (merchant id, member count, and a specific transaction id from run 1 all unchanged after run 2). This is the correct behavior and **would fully fix CRITICAL-1** if the Dockerfile actually invoked it with the flag.
- **Partial/interrupted-seed scenario (empirically reproduced, see MINOR-3):** manually inserted a bare `Merchant` row with zero `TeamMember`/`Member`/`Transaction` rows, then ran `seed_if_empty()` — it incorrectly treated this as "already has data" and no-op'd, leaving a permanently broken, unloginnable merchant. This specific interleaving can't arise from a crash *inside* `seed()` itself (which is one all-or-nothing transaction with rollback-on-exception), so it requires an out-of-band Merchant insert to trigger — narrow precondition, low-to-moderate severity, documented as MINOR-3 above rather than a blocker.

---

## (f) Live adversarial testing — methodology and full results

All live testing used a real running `uvicorn` process (either an in-process background-thread server per-request-session technique matching `test_redemption_concurrency.py`, or an actual `uvicorn --workers 3` multi-process subprocess for the strongest concurrency test), never the shared-session `TestClient` fixture, to get genuine per-request DB sessions and real concurrency.

**Webhook adversarial matrix (single run, all PASS unless noted):**
| Check | Result |
|---|---|
| Cross-merchant secret confusion (sign with B's secret, POST to A's URL) | `401` — correctly rejected |
| Control: correct secret for own merchant | `201` — correctly accepted |
| Unknown `merchant_id` | `404` |
| Wrong `Content-Type` header (`text/plain`) with correct HMAC over raw bytes | `201` — endpoint reads raw body regardless of declared Content-Type, as designed (HMAC is computed over raw bytes either way) |
| 5MB payload with valid signature | `201` in ~40ms, no size cap enforced (MINOR-4) |
| Malformed JSON | `422` |
| Missing `customer` field | `422` |
| No `Authorization` header + valid HMAC | `201` |
| Valid JWT, no/bad HMAC | `401` |
| 15 concurrent identical webhook deliveries, single-worker in-process server | 1×`201`, 14×`200 duplicate_ignored`, 1 Transaction row — race NOT triggered by timing alone |
| 20 concurrent identical webhook deliveries, real `uvicorn --workers 3` (genuine multi-process) | 1×`201`, 19×`200`, 1 Transaction row — race still NOT triggered by timing alone |
| **Forced deterministic interleaving** (barrier-synchronized two-thread replay of the exact check-then-act sequence) | **2 Transaction rows created with the same `external_order_id`; final balance `49` instead of the correct `98` for 2 real earns** — race **is** present in the code (CRITICAL-2), just narrow enough that naive concurrent-request timing doesn't reliably hit it |
| `send_sample_shopify_webhook.py` end-to-end against live seeded server | Exit code 0, `Success: created transaction id=... points=49`, balance printed correctly |

**Team management adversarial matrix (all PASS — no bugs found):**
| Check | Result |
|---|---|
| Member attempts `PATCH /team/{own_id}/role` → `admin` | `403` |
| Member attempts `POST /team/invite` with `role=admin` | `403` |
| Invite email already a TeamMember on a *different* merchant | `400` (see MINOR-2 for the minor info-leak nuance) |
| JWT re-signed with role=admin using a wrong/guessed secret | `401` |
| `alg=none` JWT forgery | `401` |
| Removed teammate's still-unexpired JWT, tried immediately after deletion | `401` (was `200` moments before deletion) — no revocation table needed since `get_current_user` does a live `db.get(TeamMember, id)` every request; a deleted row simply can't be found |
| DSN normalizer, 12 additional edge cases (query params, driver variants, malformed/empty strings) | All handled without crashing; see MINOR-1 |

Scripts used for the above are ad hoc Python files run against this repo's `app`/`scripts` modules directly (in-process live server + `httpx`/`ThreadPoolExecutor`, and a separate deterministic-barrier script for the forced-race proof); not committed to the repo since they were adversarial/exploratory, but the exact request sequences and assertions are reproducible from the tables above.

---

## (g) Overall verdict

**Batch 1 does NOT meet its own acceptance bar and should NOT ship as-is.** Test-suite hygiene is genuinely good (67/67 fresh-install pass, new tests are substantive, not shallow — see §(d)), and two of the three features (Feature 2's HMAC/auth surface, Feature 3's roles) hold up well under adversarial live testing, including the specific cross-merchant-secret-confusion attack this task flagged as top priority (verified safe). But:

1. **Feature 1 — the entire stated purpose of this batch — is not actually deployed.** The Dockerfile still unconditionally wipes the database on every restart (CRITICAL-1). This isn't a subtle gap; it's the literal bug PLAN_BATCH1.md was written to fix, left unfixed in the one file that matters for production behavior, while every unit-testable piece of the fix (the validator, the `--seed-if-empty` flag) was correctly built and tested in isolation — a classic "tested the parts, never wired the whole" gap that 67 passing pytest tests cannot catch because none of them execute the Dockerfile.
2. **Feature 2's idempotency guarantee is not actually enforced by the code**, only accidentally protected by request-timing/async-event-loop luck in the common case; a deterministic reproduction proves duplicate `Transaction` rows and a lost `points_balance` update are both possible under real concurrent redelivery (CRITICAL-2). This also surfaces a latent pre-existing gap in `earn_points()` (no atomic update, unlike the already-fixed `redeem_reward()`) that this batch's new webhook caller newly makes relevant.
3. Feature 3 (multi-user roles) is solid — no bugs found after deliberately adversarial testing (self-escalation, JWT forgery, cross-tenant leakage, lockout, revocation-on-delete).

**Counts:** 2 Critical, 0 Major (the one flagged adversarial target — cross-merchant secret confusion — was verified safe, not a bug), 4 Minor.

**Recommendation:** Do not deploy to the infra stage (#25) until CRITICAL-1 (one-line Dockerfile fix) and CRITICAL-2 (unique constraint + atomic update fix) are addressed; both are well-scoped, well-understood fixes, not open design questions. Feature 3 can ship as-is.
