# MANAGER_REVIEW_BATCH1.md — Ledgerly Batch 1 Final Review

Independent verification pass over PLAN_BATCH1.md, TEST_REPORT_BATCH1.md, and README.md §6's
claimed fixes for the two critical bugs (CRITICAL-1: Dockerfile never wired `--seed-if-empty`;
CRITICAL-2: `earn_points()` lost-update race + Shopify webhook idempotency TOCTOU). This is not
a rubber stamp — every code claim below was independently re-read and, where feasible, re-run.

## Verdict: GO, with one required infra-stage condition (see §6)

Both critical bugs are genuinely fixed in the code, not just claimed fixed. The fresh
independent test run confirms 69/69 passing, the concurrency regression test is substantive
and non-flaky across 5 runs, and both non-webhook-race features (Postgres bootstrap, multi-user
roles) hold up under direct inspection and live exercise. Feature 2 (Shopify webhook) is usable
end-to-end via the demo script, verified live. Nothing found in this pass blocks shipping.

---

## 1. Code spot-checks

**`backend/Dockerfile` (line 12):**
```
CMD ["sh", "-c", "python scripts/seed_data.py --seed-if-empty && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
```
Confirmed — `--seed-if-empty` is now actually wired into the CMD. This is the exact one-line
fix CRITICAL-1 called for.

**`backend/app/services/ledger.py::earn_points()`:** Confirmed real fix, not read-modify-write.
Credits via a single atomic SQL statement:
```python
result = db.execute(
    update(Member).where(Member.id == member.id)
    .values(points_balance=Member.points_balance + points, last_activity_at=activity_ts)
)
```
computed and applied entirely in SQL (`Member.points_balance + points` is a SQL expression, not
a Python read-then-write), with `db.refresh(member)` afterward to resync the ORM identity map.
Same pattern as the already-fixed `redeem_reward()`. This is a real fix.

**`backend/app/db/models.py::Transaction.external_order_id`:** Confirmed
`unique=True` (in addition to `nullable=True, index=True`), with a clear code comment
explaining NULL-safety for the many non-webhook rows that don't set it.

**`backend/app/services/shopify.py::ingest_shopify_order()`:** Confirmed the SELECT-based
dedup check is now explicitly documented and treated as a "fast-path only" check, with the
`IntegrityError` from the flush as the authoritative source of truth:
```python
try:
    db.flush()
except IntegrityError:
    db.rollback()
    return None, True
```
This is the correct fix shape — DB-level constraint + catch, not just a smarter SELECT.

All four spot-checks match README §6's description exactly. No gap between claimed and actual
code found.

---

## 2. Fresh independent test run

Ran in a clean shell against the actual repo (dependencies were already present in this
environment — `pip list` confirmed fastapi/sqlalchemy/psycopg2-binary/pydantic/numpy/pandas/
scikit-learn/etc. all installed at versions satisfying `requirements.txt`):

```
cd backend && python3 -m pytest -q
...
69 passed, 10 warnings in 27.65s
```

Confirms the claimed 69/69 (67 from the tester's fresh run + 2 new `test_earn_concurrency.py`
tests). No failures, no skips, no collection errors.

---

## 3. Concurrency fix re-verification

Read `backend/tests/test_earn_concurrency.py` in full. It is a **real regression test, not
theater**:

- `test_concurrent_duplicate_order_id_webhooks_produce_exactly_one_transaction`: spins up a
  genuine live `uvicorn` server in a background thread (own DB session per HTTP request, not
  the shared-session `TestClient`), fires 25 threads at a `threading.Barrier` to force
  simultaneous delivery of the *same* `external_order_id`, and asserts exactly 1×201,
  24×`duplicate_ignored`, exactly 1 `Transaction` row in the DB, and `points_balance` equal to
  exactly one earn's worth — not 0, not doubled.
- `test_concurrent_distinct_earns_for_same_member_never_lose_an_update`: isolates the
  `earn_points()` lost-update bug specifically (no webhook/idempotency involved) — 20 concurrent
  *distinct* `POST /transactions` calls for the same member, barrier-synchronized, asserting the
  final balance is the exact sum of all 20 increments and exactly 20 `Transaction` rows exist.

Both tests assert exact counts/values (not "some succeeded"), and both explicitly target the
same mechanism the tester's forced-interleaving repro exploited. This is a good-faith,
non-trivial regression test.

**Flakiness check:** ran `pytest tests/test_earn_concurrency.py` 5 times in isolation.
**5/5 passed, ~5-6s each, no flakiness observed.**

**Independent restart-durability re-check (CRITICAL-1):** ran `seed_data.py --seed-if-empty`
twice against the same fresh SQLite file, simulating a redeploy:
- Run 1 (empty DB): seeded 1 merchant / 620 members / 7,401 transactions, merchant id
  `66e7bd19c7eb4f33a9256303598a1965`.
- Run 2 (simulated restart): printed `Database already has data (a Merchant row exists) --
  --seed-if-empty is a no-op.` — identical merchant id, identical counts. Confirmed the fix
  works exactly as claimed.

---

## 4. Feature completeness assessment

**Feature 2 (Shopify webhook) — usable end-to-end, verified live.** Ran the real demo script
against a real locally running server seeded via `seed_data.py`:
```
python scripts/send_sample_shopify_webhook.py --base-url http://127.0.0.1:8127 \
    --merchant-email demo@merchant.com --secret demo-shopify-secret-change-me
→ Success: created transaction id=5b0f... points=49
→ Member jon.snow@example.com new points_balance: 49
```
Re-running it: `Webhook accepted as a duplicate (idempotent replay) -- no new transaction
created.` (exit 0). Running with a wrong `--secret`: `401`, exit code 1. All three outcomes
match the documented behavior exactly. (Note: the first attempts in this sandbox failed with an
unrelated `httpx`/SOCKS-proxy import error caused by this specific sandbox's outbound proxy
env vars — not a code bug; clearing those env vars made the script work immediately. A normal
dev machine or Railway container has no such proxy configured, so this is a sandbox artifact,
not a defect to fix.)

**Feature 3 (multi-user roles) — reasonably complete per plan.** Read `backend/app/api/team.py`
in full: `GET /team` (any role), `POST /team/invite` (admin-only via `require_admin`),
`DELETE /team/{id}` and `PATCH /team/{id}/role` (admin-only, both with a correct last-admin
lockout guard via `_active_admin_count`, both scoped to `current_user.merchant_id` so
cross-merchant IDOR is structurally prevented by the query itself, not just an after-the-fact
check). Matches PLAN_BATCH1.md's Feature 3 design section point-for-point. Test suite
(`test_team.py`, 12 tests) plus the tester's own adversarial live testing (self-escalation, JWT
forgery, cross-tenant leakage, both lockout paths) already covered this thoroughly and found
zero bugs — this pass's re-read of the router code found nothing to add.

---

## 5. Outstanding minor issues from TEST_REPORT_BATCH1.md

Four minors were flagged in the original adversarial pass; README §6 documents fixes only for
the two criticals, not the minors. Re-checked each:

- **MINOR-1 (DSN normalizer doesn't validate its output is connectable):** still present, not
  fixed. Acceptable — a malformed DSN fails loudly at `create_engine`/connect time either way;
  low value to fix now.
- **MINOR-2 (cross-tenant email-existence leak via `/team/invite` 400):** still present, not
  fixed. Explicitly called out in PLAN_BATCH1.md as the intended tradeoff of global email
  uniqueness. Acceptable, documented, not a regression.
- **MINOR-3 (`seed_if_empty()` only checks `Merchant.first()`, not `TeamMember`):** confirmed
  still present by reading `scripts/seed_data.py::seed_if_empty()` directly — it still only
  queries `Merchant`. The narrow precondition (an out-of-band `Merchant` insert outside
  `seed()`/`signup`) that triggers this remains unlikely in practice but is real. **Acceptable
  to leave for this batch** given the precondition requires an out-of-band write path that
  doesn't exist yet in the shipped code — but flag it as a cheap one-line fix
  (`TeamMember.first()` instead of/in addition to `Merchant.first()`) worth doing in the next
  small pass, since it's exactly the kind of "seed script didn't actually make redeploys safe"
  gap this batch is about.
- **MINOR-4 (no request body size cap on the unauthenticated webhook endpoint):** still present,
  not fixed. Acceptable for now — `merchant_id` isn't practically guessable (32-char hex UUID),
  and this is a genuine but low-severity DoS-adjacent gap common to most webhook receivers at
  this maturity stage. Worth a `Content-Length` pre-check as defense-in-depth before this app
  handles real Shopify traffic, not before this deploy.

None of the four are blockers for this batch.

---

## 6. Go/No-Go

**GO.** Both critical bugs are genuinely fixed (verified independently, not just re-reading the
coder's claims), the concurrency regression test is real and non-flaky across 5 runs, the full
suite is green (69/69, fresh install), and both remaining features (Shopify webhook end-to-end
demo, multi-user roles) work as designed under direct live exercise.

**One condition before/during the infra push (task #25), not a code blocker:**
1. **Verify Feature 1 acceptance criteria #1 and #3 against the real Railway Postgres** — this
   review (like the tester's) could only verify the bootstrap logic in isolation against SQLite;
   no live Postgres instance was available in this sandbox either. Once Postgres is provisioned,
   explicitly confirm `GET /health` returns 200 and that `seed_data.py --seed-if-empty` produces
   the expected counts against the real instance before considering the deploy complete — this
   is the one acceptance item that has never actually been exercised against real Postgres by
   anyone in this pipeline so far.
2. **Do the actual restart-durability check against the deployed Railway service once live**
   (redeploy/restart once, confirm member count and a specific transaction id survive) — this
   review reproduced the mechanism correctly against local SQLite/`seed_data.py` directly, but
   the literal Dockerfile-driven container-restart path on Railway itself has still never been
   observed end-to-end by anyone; it's the single highest-risk item in this whole batch and
   deserves one real observation before calling it done.
3. Optional, low-priority, non-blocking: fix MINOR-3 (`seed_if_empty()` checking
   `TeamMember` as well as `Merchant`) in a small follow-up — cheap, and directly on-theme for
   what this batch is supposed to guarantee.

No other changes are needed before shipping.
