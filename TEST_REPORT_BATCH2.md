# TEST_REPORT_BATCH2.md — Independent Tester Verification, Batch 2 (Future Value + Next Best Product)

Status: COMPLETE

Date: 2026-08-03
Tester: independent QA pass (fresh re-run, not trusting coder's report)

---

## 0. Plan read

Read PLAN_BATCH2.md in full. Key acceptance criteria and highest-risk item identified:
`mint_points=false` default on CSV upload — uploaded historical transactions must NOT
increment `Member.points_balance` unless `mint_points=true` is explicitly passed.
This is called out in the plan itself as "the single highest-risk design choice in this batch."

Also flagged for adversarial testing: cross-tenant CSV upload (can merchant A upload data
that lands in merchant B's account?), CSV validation edge cases (empty file, header-only,
BOM/encoding, huge file, in-file duplicate external_order_id), and consistent
auth/role-gating on new insights endpoints.

## 1. Test suite re-run — CONFIRMED PASS

`pip install --break-system-packages -q -r requirements.txt` succeeded cleanly.
`python3 -m pytest -q --collect-only` → **114 tests collected** (matches coder's claim of 69+45).

Ran fresh (not reusing coder's run). Sandbox tool timeout is 45s per call, same limit the coder
hit, so split into 3 invocations by test file (sandbox artifact, not a bug):
- Batch 1 (`test_auth`, `test_config`, `test_csv_ingest`, `test_earn_concurrency`, `test_fraud_detector`, `test_future_value`): **38 passed**
- Batch 2 (`test_insights_api`, `test_ledger`, `test_members`, `test_next_best_product`): **37 passed**
- Batch 3 (`test_churn_model`, `test_recommender`, `test_redemption_concurrency`, `test_shopify_webhook`, `test_team`, `test_transactions`): **39 passed**

Total: 38+37+39 = **114/114 passed, 0 failures, 0 errors**. Independently confirmed.

## 2. New test file review — PASS (substantive, not shallow)

Read `test_csv_ingest.py` (16 tests, service-layer), `test_insights_api.py` (19 tests,
HTTP-layer), skimmed `test_future_value.py` (8 tests) and `test_next_best_product.py` (5 tests)
in full via grep of test names + key bodies.

These are genuinely substantive, not rubber-stamp tests:
- `test_csv_ingest.py` covers: valid ingest + member auto-create, malformed-row skip with
  **correct 1-based row numbers** (explicitly asserted, not just counted), missing-required-
  header file-level rejection, empty file, amount-out-of-range, invalid email, **mint_points
  default-false leaves balance unchanged** + **mint_points=true credits exactly the right
  amount**, cross-upload idempotency AND **duplicate external_order_id within the same file**
  (both cases, correctly distinguished from each other), row-without-external_order_id never
  falsely flagged duplicate, channel-defaults-to-pos, and "one bad row doesn't abort the rest."
  This list maps almost 1:1 onto the plan's row-level validation rules.
- `test_insights_api.py` covers upload (success/bad-header/malformed-rows/no-auth/mint-toggle/
  idempotency/non-csv-rejection), future-value (list, horizon param, per-member, unknown-404,
  **cross-merchant-404**, no-auth-401), next-best-product (no-data graceful, unknown-404, and
  critically **the before/after upload granularity-flip test** — seeds a redemption first so
  the pre-upload state is genuinely "category" not just "empty", then uploads product data and
  asserts the flip to "product"), and `report.csv` (schema + auth).
- `test_future_value.py` has `test_loyal_cohort_scores_higher_than_lapsing_cohort_on_average`
  and `test_both_trained_and_heuristic_paths_appear_in_seeded_data` — i.e. the coder already
  wrote the exact directional-sanity and split-coverage tests this pass was asked to
  independently verify (see §5 below, where I reproduced these results live rather than just
  trusting the test).

**Gap found:** no test exercises `MAX_UPLOAD_ROWS = 20_000` (the DoS guard mentioned in the
plan's file-level validation rules). Confirmed via source read that the check exists
(`app/services/csv_ingest.py` line ~128: `if len(rows) > MAX_UPLOAD_ROWS: raise
CsvUploadError(...)`), so it is not a code bug, just an untested code path. Minor.

## 3. mint_points=false verification (HIGHEST PRIORITY) — PASS

Live-ran backend (`uvicorn app.main:app`) against a freshly reseeded demo DB (`python3
scripts/seed_data.py`, 620 members / 7123 txns / 182 redemptions), logged in as
`demo@merchant.com` (admin), and directly exercised the CSV upload path end to end.

**Code review of `app/services/csv_ingest.py`:** when `mint_points=False` (the default), the
service constructs `Transaction(...)` directly (not via `earn_points()`), explicitly sets
`points=0`, and never touches `Member.points_balance` or `Member.last_activity_at`. Only when
`mint_points=True` does it call `app.services.ledger.earn_points()` (the real, atomic-UPDATE
ledger path). The `mint_points` param is a `Query(False, ...)` bound directly to
`get_current_merchant`-scoped merchant — there is no field in the CSV schema or any other
request parameter that lets a caller name a *different* merchant, and no alternate code path
that bypasses this. This is the single design decision the plan flagged as highest-risk, and
it is implemented correctly.

**Live verification (not just code review):**
- Captured `points_balance` for all 6 real seeded members appearing in
  `scripts/fixtures/sample_product_transactions.csv` (7 emails/42 rows) BEFORE upload:
  `{'john21@example.net': 79, 'fjohnson@example.org': 1864, 'jennifermiles@example.com': 62,
  'blakeerik@example.com': 227, 'curtis61@example.com': 603, 'blairamanda@example.com': 655}`.
- `POST /api/v1/insights/upload` (default `mint_points`, i.e. false) with the fixture CSV →
  `200`, `{"rows_received":42,"rows_ingested":42,"rows_skipped_duplicate":0,"rows_failed":0,
  "members_created":0,"errors":[]}`.
- Re-fetched the same 6 members' balances AFTER upload: **byte-for-byte identical** to before
  — `CHANGED: {}`. Confirmed no minting occurred.
- Re-uploaded the **identical file a second time**: `{"rows_ingested":0,
  "rows_skipped_duplicate":42, "rows_failed":0}` — fully idempotent via `external_order_id`,
  matches acceptance criterion #4 exactly.
- Adversarial check requested by the brief — "is there any way, intentional or accidental, to
  make upload mint points": uploaded a **fresh** one-row CSV (new `external_order_id`,
  `amount_usd=40.00`) for `blairamanda@example.com` with `?mint_points=true` explicitly passed.
  Balance went from **655 → 695** (exactly +40, matching the documented 1:1
  `points_per_dollar` floor rule) — confirms the opt-in path works correctly and *only* fires
  when explicitly requested via the query param, gated the same way as every other
  merchant-scoped write in this app (JWT + `get_current_merchant`; no separate admin-role gate,
  but that is consistent with the existing convention — see §7 below, `POST /transactions` and
  `POST /rewards/redeem` are likewise not admin-gated, only team-management endpoints in
  `team.py` are).

**Verdict: no regression. This is the most consequential thing in the batch and it is solid.**



## 4. CSV validation adversarial testing — PASS (all edge cases handled correctly)

All tested live against the running server (not just unit tests):

| Case | Result |
|---|---|
| Empty file (0 bytes) | `422 {"detail":"Uploaded file is empty."}` — nothing ingested |
| Header-only CSV (valid headers, 0 data rows) | `200 {"rows_received":0,"rows_ingested":0,...}` — no crash, sensible no-op |
| CSV with UTF-8 BOM (`\xEF\xBB\xBF` prefix) | `200`, row ingested correctly — service explicitly decodes with `utf-8-sig`, which strips the BOM; confirmed this isn't accidental (real code path, real member created) |
| Missing `amount_usd` header entirely | `422 {"detail":"Missing required column(s): amount_usd"}` — whole file rejected, matches acceptance criterion #7 exactly |
| Non-CSV file (`.txt` extension, `text/plain` content-type) | `422 {"detail":"File must be a .csv / text/csv file."}` |
| Malformed rows (bad date / bad amount) mixed with valid rows | `200`, good rows ingested, bad rows individually reported with correct 1-based row numbers (also unit-tested) — one bad row never aborts the file |
| **Duplicate `external_order_id` WITHIN the same uploaded file** (not just across uploads) | Live-tested with a 2-row file sharing one `external_order_id`: `{"rows_received":2,"rows_ingested":1,"rows_skipped_duplicate":1,"members_created":1}` — idempotency holds within a single file, not just across re-uploads. The service tracks `seen_external_order_ids` as an in-request set specifically for this. |
| **Cross-tenant upload** (can merchant A's upload land in merchant B's account, or affect merchant B's real members?) | Live-tested: created a second, independent merchant ("Rival Co") via signup, logged in as its admin, and uploaded a CSV containing `blairamanda@example.com` — an email that is a real member under the **demo** merchant. Result: rival's upload created a **brand-new, separate `Member` row under the rival's own `merchant_id`** (`members_created: 1`, new member id `14056a9148f84a22b884eaeb9092e35a`), and the demo merchant's real `blairamanda@example.com` balance was **completely unaffected** (still 695, unchanged from the mint_points test in §3). This is correct, structurally-guaranteed tenant isolation — the upload endpoint has no `merchant_id` field anywhere in its request surface; the merchant is always derived from the JWT via `get_current_merchant`, and member lookup/creation in `csv_ingest.py` is always scoped by `Member.merchant_id == merchant.id`. There is no way, intentional or accidental, to upload into another merchant's account via this endpoint. |
| Huge file / `MAX_UPLOAD_ROWS=20_000` DoS guard | Verified via source code only (not exercised live — generating a 20k+ row file was deprioritized given time budget): `app/services/csv_ingest.py` rejects with `CsvUploadError` before any row is processed if `len(rows) > MAX_UPLOAD_ROWS`. No test exercises this in the suite either (noted as a minor gap in §2). Not a functional risk, just an untested path. |

## 5. Future-value model sanity check — PASS (strong result)

Live-queried `GET /insights/future-value` for all 620 seeded members, then cross-referenced each
member's `model_used` and `predicted_future_value` against the seed script's `synthetic_cohort`
field directly in the SQLite DB (not via any test helper — raw `sqlite3` query joined against
the live API response in Python):

```
cohort=at_risk      n=  87 model_used_counts={'trained': 87}   avg_predicted_future_value=108.13
cohort=average      n= 321 model_used_counts={'trained': 321}  avg_predicted_future_value=234.52
cohort=lapsing      n= 107 model_used_counts={'trained': 107}  avg_predicted_future_value=34.88
cohort=loyal        n=  88 model_used_counts={'trained': 88}   avg_predicted_future_value=845.98
cohort=new_member   n=  17 model_used_counts={'heuristic': 17} avg_predicted_future_value=33.86
```

- **Directional sanity: confirmed.** `loyal` (845.98) is by far the highest-scoring cohort,
  ~3.6x the merchant-wide "average" cohort, and `lapsing` (34.88) is near the bottom (`at_risk`
  is slightly above `lapsing`, which is plausible — "at risk" members are declining but not as
  far along as "lapsing" members). This is exactly the ordering the plan's honest-framing
  section says the model should produce.
- **model_used split: exactly matches the coder's claimed 603 trained / 17 heuristic.**
  603 = 620 - 17. Every one of the 17 `new_member` cohort members (seeded with "zero pre-cutoff
  activity" by design, per `scripts/seed_data.py` line ~144) falls back to `"heuristic"`, and
  every other cohort trains successfully. This is not a coincidence — it's the direct,
  mechanical consequence of `MIN_TRAINING_MEMBERS=30` easily passing at 620 members and the
  per-member fallback correctly triggering only for members with no pre-cutoff earn activity.
  Both `model_used` values are proven to appear in the live seeded response (acceptance
  criterion #1 requirement).
- **Zero negative `predicted_future_value` values** across all 620 members (also
  independently confirmed, separate from the coder's own test).
- 3 members created live during this test session via CSV upload (no `synthetic_cohort`, as
  expected for organically-created members) scored low (avg 12.02) with `trained` — sensible
  given minimal transaction history.

## 6. Next-best-product before/after upload — PASS

Before any upload, a seeded member's `next-best-product` correctly returns
`data_granularity: "category"`, `product_name: null`, populated `category` field (e.g.
`apparel`, `bonus`, `gift-card`) — degrading gracefully to the `Redemption` ×
`RewardCatalogItem.category` substrate as designed.

Live end-to-end test: created a brand-new member via CSV upload with 3 rows of real
`product_category`/`product_name` data (2x `beverage`/"Cold Brew 16oz", 1x
`merchandise`/"Ceramic Mug"), then queried `next-best-product` for that exact member. Result:
`data_granularity` flipped to `"product"` merchant-wide, and the recommendations surfaced
concrete, sensible product suggestions in categories the member had **not** yet purchased —
`bakery`/"Almond Croissant" (score 0.3347) and `grocery`/"Espresso Roast Bag 12oz" (score
0.3169) — correctly excluding `beverage` and `merchandise` (their own already-engaged
categories, filtered by the `LOW_ENGAGEMENT_THRESHOLD` logic) and correctly NOT recommending
categories at random — this is real item-based CF output, not decorative. Matches acceptance
criterion #6.

## 7. Auth/access control on insights endpoints — PASS, with one design note (not a bug)

- **Unauthenticated (`401`):** confirmed live for `POST /upload`, `GET /future-value`, and
  `GET /report.csv` — all three return `401` with no token.
- **Cross-tenant (`404`, matching the rest of the codebase's pattern):** confirmed live —
  a second, independently-created merchant ("Rival Co") gets `404` for both
  `GET /future-value/{demo_member_id}` and `GET /next-best-product/{demo_member_id}` when
  querying the demo merchant's real member id. Correct merchant-scoping via
  `Member.merchant_id == merchant.id` in `_get_member_or_404`.
- **Role gating (`member` vs `admin`):** a `role="member"` (non-admin) authenticated user
  (`demo-member@merchant.com`, seeded by the seed script for exactly this purpose) can call
  `GET /future-value` (200), `GET /next-best-product` (200), and **`POST /upload`** (200) —
  i.e. insights endpoints, including CSV upload, are **not** admin-gated. This is consistent
  with the rest of this codebase's existing convention: `require_admin` (in `app/api/deps.py`)
  is only used by `app/api/team.py` for team-management writes (invite/remove/promote
  teammates); `POST /transactions`, `POST /rewards/redeem`, and all of `ai.py` are likewise
  open to any authenticated team member regardless of role. The plan itself doesn't specify
  role-gating for `insights.py` beyond `get_current_merchant`, so this is not a regression or
  a deviation from spec. **Design note for the manager/next batch, not a bug in this batch:**
  CSV upload is an unusually consequential bulk-write action (up to 20,000 rows, and with
  `mint_points=true` a real balance-changing one) compared to the single-row writes the
  no-role-check convention was originally established for; a merchant might reasonably want
  upload restricted to admins even though the rest of the app doesn't distinguish roles for
  writes. Flagging as a suggestion, not a defect.

## 8. Acceptance criteria checklist (PLAN_BATCH2.md "Acceptance criteria" section)

1. `GET /future-value` (no upload), 620 entries, all `>=0`, both `model_used` values present — **PASS** (§5, live-verified: 603 trained / 17 heuristic, 0 negatives)
2. `GET /future-value/{member_id}` matches list entry; unknown/foreign id → 404 — **PASS** (live-verified both; cross-merchant 404 in §7)
3. `GET /next-best-product/{member_id}` (no upload) → category granularity, null product, non-empty category — **PASS** (§6, live-verified)
4. Upload fixture CSV → `rows_ingested` = valid row count, `rows_failed:0`; re-upload → all duplicate, `rows_ingested:0` — **PASS** (§3, live-verified: 42/42 then 0/42 dup)
5. Balances unchanged after default upload; `mint_points=true` on a fresh file does increase balances correctly — **PASS** (§3, live-verified, the single most important check)
6. Next-best-product flips to `"product"` granularity with real `product_name` after upload — **PASS** (§6, live-verified)
7. CSV missing `amount_usd` header → 422, nothing ingested; 10 valid + 2 bad-date rows → `rows_ingested:10, rows_failed:2`, 2 correctly-numbered errors — **PASS** (§4 live + unit tests)
8. `GET /report.csv` → 200, `text/csv`, one row per member, documented columns — **PASS** (live-verified: 624 lines = header + 623 members, matching exact live member count at test time; correct header order)
9. All endpoints reject unauthenticated (401), scoped to caller's merchant (cross-merchant → 404) — **PASS** (§7, live-verified)
10. Full suite: 69 pre-existing + 45 new = 114/114 passing — **PASS** (§1, independently re-run fresh, not reusing coder's run)
11. Frontend: `/insights` route, sorting, upload/download round-trip, zero console errors — **PARTIAL**. Independently re-ran `npm run build` (after `rm -rf dist`) — clean build, 0 errors, 0 warnings (`tsc -b && vite build`, 44 modules, built in 1.61s). Did **not** independently browser-test the live `/insights` page interactions (sorting clicks, upload button round-trip, download button, console-error check) due to time budget — this pass focused its live-testing effort on the backend/data-integrity risk surface per the task brief's explicit prioritization. Code-level read of `Insights.tsx` was not performed either. **Recommend the manager or a follow-up pass do a quick browser smoke-test of the actual page before shipping**, since this is the one acceptance criterion not directly exercised.

---

## Overall verdict

**PASS.** 114/114 tests independently re-confirmed. The plan's single highest-risk item —
`mint_points=false` not minting real points on backfill upload — was adversarially tested live
(not just code-reviewed) and holds under every variant tried: default upload, re-upload,
explicit `mint_points=true` opt-in, and a deliberate cross-tenant upload attempt using a real
member's email from a different merchant. Cross-tenant isolation on both upload and read
endpoints is structurally sound (no `merchant_id` is ever caller-supplied; everything derives
from the JWT). CSV validation handles empty files, header-only files, BOM encoding, missing
headers, malformed rows, and in-file duplicate `external_order_id`s correctly. The future-value
model's trained/heuristic split (603/17) and cohort-ordering (loyal ≫ average > at_risk >
lapsing) were independently reproduced live, not just trusted from the coder's report or
existing tests. Next-best-product's before/after granularity flip is real, working CF output.

**Issue counts:** 0 critical, 0 major, 3 minor (untested `MAX_UPLOAD_ROWS` DoS-guard path;
CSV upload not admin-gated despite being a higher-consequence bulk action than the rest of the
app's writes — design note, not a bug; frontend `/insights` page not browser-smoke-tested by
this pass).

**Single most important finding:** the `mint_points=false` default — the plan's explicitly
flagged highest-risk item — is implemented correctly and was verified live end-to-end,
including an adversarial cross-tenant attempt: a rival merchant uploading a real demo-merchant
member's email created an isolated duplicate under its own tenant rather than touching the
real member's balance, and every upload variant left non-consenting balances untouched to the
cent.

---
(Report complete.)
