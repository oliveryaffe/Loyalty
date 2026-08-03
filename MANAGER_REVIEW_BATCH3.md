# MANAGER_REVIEW_BATCH3.md — Final Review, Batch 3 (GDPR pass, Stripe billing, notifications/win-back, A/B testing)

Reviewer: manager (independent verification pass — did not write any of this code, does not
trust the coder's or tester's reports at face value, re-ran everything that could be re-run)
Date: 2026-08-03

## Verdict: **GO, with one gap that must be closed before this batch is called "done" and communicated to the owner as complete**

The five features that were adversarially tested (GDPR erasure/export, Stripe billing, notifications,
win-back, A/B testing) are in genuinely good shape — the fix pass's claims about the 5 bugs from
`TEST_REPORT_BATCH3.md` all check out under my own independent re-verification, not just a re-read of
the diff. **However, I found a sixth, unreported gap while doing my own pass**: two entire
sub-sections of the plan's own §1 (GDPR technical pass) — **self-hosted fonts on the marketing site**
and **placeholder legal pages** — were never implemented, despite the dashboard linking to pages that
don't exist. Neither the tester nor the fix pass caught this because both scoped their attention to
the four feature areas (GDPR erasure/export + billing + notifications/winback + experiments) and
never checked back against the fonts/legal-pages items in the same plan section. This is a real,
customer-visible gap in a batch whose headline purpose was a "GDPR technical pass" — but it is a
missing-page problem, not a data-integrity or security problem, so it does not block shipping the
other four features; it blocks calling §1 (the GDPR pass) fully complete. See §5 below.

---

## 1. Test suite — independently re-run from scratch, confirmed 208/208

Did not reuse the coder's or tester's test run. Used a separate pre-existing venv (`/tmp/venv`,
already had `stripe` installed), ran `pytest --collect-only` first to confirm the count, then the
full suite in 5 batches to respect the sandbox's 45s per-call timeout:

```
pytest --collect-only -q                                                    -> 208 tests collected
tests/test_auth.py test_billing.py test_churn_model.py test_config.py
  test_csv_ingest.py                                                        -> 64 passed  (27.71s)
tests/test_earn_concurrency.py test_experiments.py test_fraud_detector.py
  test_future_value.py test_gdpr.py                                        -> 48 passed  (39.08s)
tests/test_insights_api.py test_ledger.py test_members.py
  test_next_best_product.py                                                -> 37 passed  (16.94s)
tests/test_recommender.py test_redemption_concurrency.py
  test_shopify_webhook.py test_team.py                                     -> 28 passed  (15.27s)
tests/test_transactions.py test_winback.py test_notifications.py           -> 31 passed  (23.87s)
```

64 + 48 + 37 + 28 + 31 = **208. Confirmed independently: 208/208, 0 failures, 0 skips.** This matches
the fix pass's claim of 196 (the tester's baseline) + 12 new regression tests exactly.

## 2. HIGH bug fix (GDPR export omitting winback/experiment data) — live-verified with my own script, not the coder's test

The task asked me not to trust the diff or the existing regression test (`test_gdpr.py::
test_gdpr_export_includes_winback_offers_and_experiment_assignments`) and to make a real export call
myself. I wrote an independent script (not reusing any test file) that:

1. Signed up a brand-new merchant via the real HTTP API.
2. Inserted a member directly in the DB with 200 days of inactivity (guaranteed high churn score).
3. Created a reward + a win-back rule via the real API, ran `POST /winback/run` — got back a real
   `WinbackOffer` row (`offers_sent: 1`).
4. Created a real `RewardExperiment` via `POST /experiments` — the merchant's one member got bulk-
   assigned to a real arm (verified `members_assigned_b: 1`).
5. Queried the DB directly for ground truth (1 `WinbackOffer` row, 1 `ExperimentAssignment` row for
   that member).
6. Called the real `GET /members/{id}/gdpr-export` endpoint and inspected the actual JSON response.

**Result — genuinely fixed, live-verified, not just read from the diff:**

```json
"winback_offers": [
  {"id": "...", "member_id": "...", "rule_id": "...", "redemption_id": "...",
   "churn_risk_score_at_trigger": 100.0, "triggered_by": "manual", "created_at": "..."}
],
"experiment_assignments": [
  {"id": "...", "experiment_id": "...", "member_id": "...", "variant": "b", "assigned_at": "..."}
]
```

Both lists are present, both counts match the ground-truth DB query exactly (1 and 1), and the field
values are correct (`triggered_by: "manual"`, a real `redemption_id`, `variant` matching the DB row).
`app/schemas/gdpr.py::MemberExportOut` now includes `winback_offers: list[WinbackOfferOut]` and
`experiment_assignments: list[ExperimentAssignmentOut]`, and `app/api/members.py::gdpr_export_member`
queries both tables filtered by `member_id`. **This is a real fix, confirmed by a live HTTP call
against a real export, not a code-read assumption.**

Residual, lower-stakes note (not re-flagged as a blocker, but worth the owner knowing): the export
still does not include `Member.last_known_risk_band` / `risk_escalated_notified_at` (added in §3,
noted as a secondary gap in the tester's report) — churn-risk profiling state about a named individual
that arguably belongs in an Art. 15/20 export too. Small, same-shape fix as the one just verified;
did not block my GO verdict on the four tested features, but the owner should know it's still open.

## 3. MEDIUM bug fix (`team.py` gating gap) — live-verified with a forced-cancellation probe

Wrote a second independent script: signed up a fresh merchant (confirmed `trialing` status lets
`GET /members` through, 200 as expected), then forced `subscription_status = "canceled"` directly in
the DB (simulating a lapsed Stripe subscription, same technique the tester used), then hit the API
live as that merchant:

```
GET /api/v1/members            -> 402  (ordinary product route, correctly hard-locked)
GET /api/v1/rewards             -> 402  (ordinary product route, correctly hard-locked)
GET /api/v1/team                -> 402  (was 200 before the fix — CONFIRMED FIXED)
POST /api/v1/team/invite        -> 402  (was 201 before the fix — CONFIRMED FIXED)
GET /api/v1/auth/me             -> 200  (exempt, as required — must reach the lock screen)
GET /api/v1/billing/subscription -> 200  (exempt, as required — must be able to resubscribe)
POST /members (create)          -> 402  (ordinary route correctly blocked too)
GET /members/{id}/gdpr-export   -> 200  (exempt, as required — compliance doesn't pause for unpaid invoices)
POST /members/{id}/gdpr-erase   -> 200  (exempt, as required)
POST /webhooks/shopify/{id}/orders-create -> 401 (bad HMAC, never 402 — Shopify ingestion never payment-gated)
```

Every element of the plan's exemption list (auth, billing, Shopify webhooks, GDPR erase/export) is
correctly reachable while hard-locked, and every ordinary product route — including `team.py`, which
was the one confirmed gap — now correctly 402s. `app/api/team.py`'s docstring now explicitly documents
why (citing `TEST_REPORT_BATCH3.md §1`), and its write endpoints use the new
`require_admin_active_subscription` dependency, its read endpoint uses `require_active_subscription`.
**Confirmed fixed by live HTTP calls, not a diff read.**

## 4. Remaining three bugs (MEDIUM: experiment exclusion zero-recommendations; MEDIUM-LOW: traffic_split validation; LOW: end_experiment idempotency) — verified by direct code read, consistent with the fix claims

- **`recommend_for_member` zero-recommendations bug**: `app/ai/recommender.py::recommend_for_member`
  now has an explicit fallback — if the variant-exclusion filter leaves zero ranked candidates, it
  re-runs the ranking with an empty exclusion set. Code comment cites `TEST_REPORT_BATCH3.md §6`
  directly. This is the correct fix shape (steering is "best-effort," not a hard rule that should ever
  leave a member with nothing).
- **`traffic_split` validation**: `app/schemas/experiments.py::ExperimentCreate.traffic_split` now has
  `Field(default=0.5, ge=0.0, le=1.0)`. Out-of-range values will now `422` instead of silently
  producing a degenerate 100/0 split.
- **`end_experiment` idempotency**: `app/api/experiments.py::end_experiment` now short-circuits and
  returns the existing (already-frozen) state if `status == "completed"`, instead of re-stamping
  `ended_at` on a second call.

All three read as correct, minimal, targeted fixes matching the tester's exact repro steps. I did not
re-run live concurrency/edge-case probes for these three (lower severity, and the fix shape is simple
enough that a code read is sufficient confidence for MEDIUM/LOW items — the HIGH and the two MEDIUMs
above got live re-verification, which is where I judged the independent-verification effort belonged).

---

## 5. New finding (not in the tester's report, not in the fix pass): §1c/§1d of the GDPR pass were never implemented for the marketing site

The plan's §1 was framed as "GDPR technical pass" and explicitly called out the Google Fonts loading
as "the specific, court-tested issue" driving the self-hosted-fonts work (§1c), plus placeholder
privacy/terms pages (§1d). I checked both independently since the task asked me to sanity-check the
GDPR section generally, not just erasure/export.

**Dashboard (`frontend/`): done correctly.** `frontend/index.html` no longer references
`fonts.googleapis.com`/`fonts.gstatic.com` at all — confirmed by reading the file directly (no Google
Fonts `<link>` tags present) — and `npm run build` produces bundled, hashed `@fontsource/inter` woff2
files in `dist/assets/` (`inter-latin-400-normal-*.woff2` etc.), consistent with the plan's design.

**Marketing site (`marketing/`): not done at all.**

- `marketing/index.html` still contains, unchanged:
  ```html
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  ```
  — the exact third-party request the plan set out to eliminate is still live.
- `marketing/fonts/` does not exist anywhere in the repo.
- `marketing/Dockerfile` is unchanged from before this batch (`COPY index.html .` only) — even if the
  font files existed, they wouldn't reach the deployed container.
- `marketing/privacy.html` and `marketing/terms.html` do not exist. **This is the more serious half of
  the gap**: `frontend/src/components/Layout.tsx` was correctly updated to add dashboard footer links
  to `${VITE_MARKETING_URL}privacy.html` and `terms.html` (the code for this part is right, and
  `VITE_MARKETING_URL` is wired as documented) — but those links point at pages that were never
  created. In production, every merchant who clicks "Privacy" or "Terms" from the dashboard footer
  gets a 404. A B2B SaaS with literally no visible privacy policy or terms of service, and a dashboard
  that links to a 404 where one should be, is a worse look than the Google Fonts issue this section
  was meant to fix in the first place.

**Why I'm not calling this a blocker for the four adversarially-tested features (billing, GDPR
erasure/export, notifications/win-back, A/B testing):** it's isolated to two specific, additive
sub-items of §1 that don't touch any of the tested business logic, data model, or security surface —
nothing here is broken, unsafe, or regressed; two files and a Dockerfile line simply weren't written.
**Why I am flagging it clearly rather than letting it slide:** the plan itself, the tester's report,
and the fix pass all treated "the GDPR technical pass" as if it shipped complete. It didn't. If the
owner or anyone else is told "Batch 3's GDPR pass is done," that statement is currently false for two
of its four sub-items. This should be closed (or explicitly deferred with the owner's sign-off) before
anyone describes this batch as GDPR-complete.

## 6. Pricing tiers and billing gating design — implementation matches what was agreed

Not relitigating the business decision (told this was already confirmed with the owner) — just
confirming the code does what the plan says.

- **Pricing**: `frontend/src/components/SubscriptionGate.tsx::TIER_PLANS` — Starter £49/mo (up to
  1,000 members), Growth £149/mo (up to 10,000 members), Scale £399/mo (unlimited) — matches the plan
  exactly, byte-for-byte, and is the single source of truth `Billing.tsx` renders from (no duplicated/
  drifting copy). `app/services/billing.py::TRIAL_PERIOD_DAYS = 14` and
  `subscription_data={"trial_period_days": TRIAL_PERIOD_DAYS}` on the Checkout Session confirms the
  card-required-upfront 14-day trial as specified.
- **Gating**: `app/api/deps.py::ALLOWED_SUBSCRIPTION_STATUSES = {"trialing", "active", "past_due"}` —
  `past_due` correctly soft-locks (stays in the allowed set; the frontend's `SubscriptionGate.tsx`
  shows a banner, not an interstitial, for this status specifically), while `canceled`/`unpaid`/
  `incomplete_expired`/`None` fall through to the hard 402 lock. Live-verified in §3 above: the
  exemption list (auth, billing, Shopify webhooks, GDPR erase/export) is real and complete, and
  `team.py` — the one previously-missing router — is now correctly included in the hard-lock set.
  **Note that no seat-cap or per-tier feature enforcement exists anywhere in the code** — the
  "2/5/unlimited seats" and "Growth+ gets Insights/notifications/winback, Scale gets A/B testing"
  distinctions in the pricing table are not technically enforced (any subscribed merchant on any tier
  can use any feature). This was true in the tester's report too and isn't a regression, but it's
  worth the owner explicitly knowing this is a packaging/marketing distinction only right now, not an
  API-level entitlement system — a Starter-tier merchant paying £49/mo can technically run A/B tests
  and win-back campaigns meant to be Scale-tier (£399/mo) features today.

## 7. GDPR erasure approach — confirmed: this is anonymization, not deletion

Read `app/api/members.py::gdpr_erase_member` directly. **What actually happens on
`POST /members/{id}/gdpr-erase`:**

- `first_name` → `"Erased"`, `last_name` → `"Member"`, `email` →
  `f"erased-{member.id}@deleted.ledgerly.invalid"`
- `is_active` → `False`
- `erased_at` → current timestamp
- **The `Member` row itself, and every `Transaction`, `Redemption`, and `FraudAlert` row that
  references it, are left in the database, fully intact, still linked by the same foreign keys.**
  Nothing is deleted. `points_balance` and every historical transaction/redemption amount remain
  exactly as they were before erasure.
- Idempotent (second call is a no-op, confirmed by both the regression test and reading the
  `erased_at is not None` early-return).

**I want to be unambiguous about this for anyone reviewing this later, including a customer's own
DPO or a regulator: this is anonymization of the directly-identifying fields (name, email), not
erasure of the member's data footprint.** The member's full transaction history, redemption history,
fraud-alert history, and points balance remain in the database indefinitely, attributed to a
now-pseudonymous row. The plan's own reasoning for this trade-off is sound from an engineering
standpoint (a real cascade-delete would corrupt the merchant's own aggregate revenue/AI-training data,
and `FraudAlert.member_id`/`Redemption.member_id` are non-nullable FKs that would need a schema change
to support a true hard-delete). Whether "overwrite name+email, keep everything else forever" actually
satisfies a specific data subject's Art. 17 erasure request, or whether a regulator/DPO would expect a
retention/purge schedule for the anonymized rows, a documented legal basis for keeping pseudonymized
transaction history indefinitely, or zeroing of `points_balance`, are legal judgment calls the plan
itself already flags as open questions for a solicitor — **I am not making that call, only making sure
it's visible and unambiguous to whoever reads this: what ships today is anonymize-in-place, not
delete, and every historical record about an "erased" member is still fully queryable by
`member.id`.**

## 8. Frontend build — clean

```
$ npm run build
✓ built in 4.26s
```

Ran this myself (deleted `dist/` and `tsconfig.tsbuildinfo` first to force a real rebuild, not a
cached one). Zero TypeScript errors, zero Vite warnings, self-hosted `@fontsource/inter` woff2 assets
correctly bundled/hashed as noted in §5 above.

## 9. Documentation — README.md was not updated for Batch 3 (flagging as a gap, not a blocker)

The repo's convention (visible in the current `README.md`) is a numbered section per batch: `## 2a.
Batch 1 additions`, `## 2b. Batch 2 additions`. **There is no `## 2c. Batch 3 additions` section, and
no mention anywhere in the README of GDPR erasure/export, Stripe billing, notifications, win-back
campaigns, or A/B testing** — the only hit for "billing" in the whole file is one incidental mention
in a future-work list. There is no separate `backend/README.md`; the root `README.md` is the single
canonical doc for this project. This should be filled in before/around ship (new endpoints, new env
vars, new `scripts/` usage if any, the exemption list, the anonymize-not-delete framing from §7 above)
so the next person onboarding to this codebase — or the next coder pass — isn't relying on
`PLAN_BATCH3.md` alone.

---

## Summary for the owner

**What's solid and independently re-verified, not just re-stated from the tester's report:**
- 208/208 tests pass, re-run from scratch, same result.
- The HIGH bug (GDPR export omitting win-back/experiment data) is genuinely fixed — I made a real
  export call against real data I created myself and got the correct payload back.
- The `team.py` billing-gating gap is genuinely fixed — I forced a cancelled subscription and hit the
  API live; every exempt route stayed reachable, every non-exempt route (including `team.py` now)
  correctly 402'd.
- The three remaining MEDIUM/MEDIUM-LOW/LOW bugs all have correct, targeted code fixes matching the
  tester's exact repro steps.
- Pricing tiers and billing gating logic in code match what was agreed with the owner.
- GDPR erasure is confirmed to be anonymization-in-place, not deletion — flagging clearly per your
  instructions, since that distinction matters if this is ever scrutinized by a regulator or a
  customer's own DPO.
- Frontend builds clean.

**What needs attention before this batch is described as "done":**
1. **Close the gap, or explicitly defer it with sign-off**: the marketing site still loads Google
   Fonts directly (the exact issue §1 was meant to fix is still live in production), and
   `marketing/privacy.html`/`terms.html` don't exist even though the dashboard now links to them —
   those links currently 404. This is new information from my pass, not in the tester's report.
2. Update `README.md` with a Batch 3 section (documentation gap, not a functional blocker).
3. Lower-priority, non-blocking: the GDPR export still omits `last_known_risk_band`/
   `risk_escalated_notified_at`; no seat-cap or per-tier feature enforcement exists in the API despite
   the pricing table implying tier-gated features.

None of the open items touch the tested business logic, security, or data-integrity surface of the
four features this batch actually shipped — that surface held up under real concurrent-request races,
real cross-tenant IDOR attempts, and a real legacy-schema drift simulation, none of which I found
reason to distrust on my own pass. **Recommendation: GO for the four tested features (GDPR erasure/
export, Stripe billing, notifications/win-back, A/B testing). Do not describe §1's font/legal-pages
work as complete until item 1 above is actually closed.**
