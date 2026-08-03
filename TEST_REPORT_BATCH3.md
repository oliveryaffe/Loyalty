# TEST_REPORT_BATCH3.md — Adversarial QA of Ledgerly Batch 3

**Status: COMPLETE.**

Scope: GDPR technical pass, Stripe billing, notifications + win-back
campaigns, A/B testing. Coder claim: 196/196 tests passing (114 original +
10 GDPR + 35 billing + 24 notifications/winback + 13 experiments).

This report documents commands actually run and their actual output, not
just conclusions. Bugs are severity-ranked at the end: CRITICAL / HIGH /
MEDIUM / LOW.

---

## 0. Test suite verification

`pytest --collect-only -q` confirms **196 tests collected** (matches the
claimed total). Ran in 4 batches (sandbox tool-call timeout is 45s, full
suite takes ~115s):

```
tests/test_auth.py tests/test_billing.py tests/test_churn_model.py tests/test_config.py tests/test_csv_ingest.py
  -> 64 passed in 28.60s
tests/test_earn_concurrency.py tests/test_experiments.py tests/test_fraud_detector.py tests/test_future_value.py tests/test_gdpr.py
  -> 38 passed in 33.77s
tests/test_insights_api.py tests/test_ledger.py tests/test_members.py tests/test_next_best_product.py
  -> 37 passed in 17.27s
tests/test_recommender.py tests/test_redemption_concurrency.py tests/test_shopify_webhook.py tests/test_team.py tests/test_transactions.py tests/test_winback.py tests/test_notifications.py
  -> 57 passed in 34.54s
```

**64 + 38 + 37 + 57 = 196. VERIFIED: 196/196 pass, claim is accurate.**
This is a real, clean pass — not a flaky/partial run. (Had to `pip install
stripe` into the pre-existing `/tmp/venv` first; it was missing from that
venv though present in `requirements.txt` — trivial environment gap, not
a code bug.)

The rest of this report is about what passing tests don't tell you.

---

## 1. Billing gating blast radius

**Method:** grepped every `Depends(...)` on every router in `backend/app/api/`,
then live-tested the hard-lock boundary with a real cancelled merchant.

Dependency audit (`grep -n "Depends(" app/api/*.py`):

| Router | Dependency used | Correct? |
|---|---|---|
| `auth.py` (signup/login/me) | `get_db` only / `get_current_user` | Yes — never paywalled, as required (must be able to log in to reach the lock screen) |
| `billing.py` (checkout/portal/subscription) | `require_admin` / `get_current_user` | Yes — never `require_active_subscription`, so a locked merchant can still resubscribe |
| `billing.py` (`/webhook`) | none (Stripe-signature-verified) | Yes — no JWT dependency at all, correct for a third-party callback |
| `webhooks.py` (Shopify ingestion) | `get_db` only | Yes — never paywalled, matches the plan's explicit rationale (never lose a real shopper's points over a billing lapse) |
| `members.py` GDPR erase/export | `require_admin` (not `require_active_subscription`) | Yes — compliance-sensitive endpoints stay reachable when hard-locked |
| `members.py` (everything else), `rewards.py`, `transactions.py`, `insights.py`, `ai.py` | `require_active_subscription` | Yes |
| `settings.py`, `winback.py`, `experiments.py` | `require_active_subscription` / `require_admin_active_subscription` | Yes |
| **`team.py`** (GET/invite/delete/role) | `get_current_user` / `require_admin` — **never `require_active_subscription`** | **Gap — see below** |

Live proof (`/tmp/team_gating_probe.py`): signed up a merchant, forced
`subscription_status="canceled"` directly in the DB (simulating a
cancelled Stripe subscription), then hit the API as that merchant:

```
GET /members (should be 402):  402 {"detail":"An active subscription is required..."}
POST /rewards (should be 402): 402 {"detail":"An active subscription is required..."}
GET /team:                     200
POST /team/invite (as cancelled/unpaid merchant): 201 {"id":"...","email":"teammate-...@example.com",...}
DELETE /team/{id} (as cancelled/unpaid merchant):  204
```

**Confirmed: ordinary product routes correctly hard-lock at 402, but
`app/api/team.py` does not — a cancelled/unpaid merchant admin can still
invite and remove teammates indefinitely.**

**Is this a real bug?** Yes, but a low-impact one, and I want to be
explicit about the reasoning both ways:

- **Why it's a real bug, not a judgment call:** `app/api/deps.py`'s
  `require_active_subscription` docstring lists the *complete, deliberate*
  exemption list — auth, billing, Shopify webhooks, GDPR erase/export.
  `team.py` is not on that list. Nothing in the plan or the code
  documents team management as an intentional exemption; it reads as an
  oversight (the coder pass literally flagged it as unresolved, per this
  task's brief), not a documented design decision the way the other four
  exemptions are.
- **Why the practical blast radius is small:** gating in this codebase is
  per-*merchant*, not per-*seat* — every teammate, existing or newly
  invited, still hits `require_active_subscription` on every real product
  route once the merchant is hard-locked. Inviting a new teammate while
  cancelled doesn't unlock any additional product functionality; the new
  teammate would get the exact same 402 interstitial the admin sees.
  There's no seat-based billing enforcement in this codebase at all today
  (the pricing table's "2/5/unlimited seats" caps aren't enforced
  anywhere, gated or not), so this isn't a revenue-bypass vector either.
- **Net:** I'd classify this **MEDIUM** severity — a real, confirmed
  inconsistency with the stated design that should be fixed before
  ship (one-line change: swap `get_current_user`/`require_admin` for
  `require_active_subscription`/`require_admin_active_subscription` in
  `team.py`, same as `winback.py`/`experiments.py`), but not a critical
  security or billing-bypass hole given the per-merchant gating model.

---

## 2. GDPR export completeness

**Static check:** `app/schemas/gdpr.py::MemberExportOut` only carries
`member`, `transactions`, `redemptions`, `fraud_alerts`, `exported_at`.
Its own docstring says winback/experiment fields were "intentionally"
left out because "those features ... are separate, not-yet-implemented
coder passes" — but by the time all four Batch 3 passes landed, both
`WinbackOffer` and `ExperimentAssignment` tables exist and hold real
per-member data (which reward a specific member was comped, at what
churn score, and which A/B arm a member was placed in). This docstring
is now stale, and nobody went back to wire the fields in.

Also missing from the export: `Member.last_known_risk_band` and
`Member.risk_escalated_notified_at` (added in §3) are not present in
`MemberOut`/`MemberExportOut` either — this is churn-risk profiling
state about a named individual, arguably personal data in its own right.

**Live proof** (`/tmp/gdpr_export_probe.py`): created a merchant, a
member, granted that member a real win-back offer via
`POST /winback/run`, created a real A/B experiment that assigned that
same member to variant B, then called
`GET /members/{id}/gdpr-export`:

```
winback run result: {'offers_sent': 1, 'member_ids': ['6143de...']}
experiment created: {..., 'members_assigned_a': 0, 'members_assigned_b': 1}

=== GDPR EXPORT RESPONSE ===
{
  "member": { ...no last_known_risk_band, no risk_escalated_notified_at... },
  "transactions": [],
  "redemptions": [ { "id": "...", "reward_id": "...", "points_spent": 0, "status": "completed" } ],
  "fraud_alerts": [],
  "exported_at": "2026-08-03T16:46:16.612959Z"
}

Top-level keys in export: ['member', 'transactions', 'redemptions', 'fraud_alerts', 'exported_at']
winback_offers key present: False
experiment_assignments key present: False
winback offers actually sent for this member (ground truth): 1

*** CONFIRMED GAP: member has a real WinbackOffer row (free reward granted)
    but the GDPR export contains no winback_offers field at all. ***
*** CONFIRMED GAP: member has a real ExperimentAssignment row but the GDPR
    export contains no experiment_assignments field at all. ***
```

Note the `redemptions` list *does* include the comped win-back
redemption itself (points_spent=0, status=completed) — so the fact that
a free reward was granted is technically inferable — but there is no way
to see it was a *win-back* offer (vs. a normal redemption), what churn
score triggered it, which rule fired, or that the member was ever
enrolled in an A/B experiment at all. `RedemptionOut` in the export
doesn't even surface the new `source="winback"` column.

**Verdict: this is a genuine, live, verified GDPR Art. 15/20 compliance
gap introduced by this batch, not a hypothetical.** A subject-access
request today would omit real personal data (win-back eligibility/grant
history, A/B cohort assignment, churn-risk-escalation/notification
state) that exists in the system about that member. Severity: **HIGH**
— this is exactly the kind of gap that turns into a real regulatory
complaint, and the fix is small (add two fields to `MemberExportOut`,
query the two new tables, same pattern as the three fields already
there) — there's no design obstacle, just a pass that didn't circle
back once the dependency landed.

---

## 3. Stripe webhook idempotency and signature verification

**Design review** (`app/api/billing.py::stripe_webhook`,
`app/services/billing.py`):

- Raw body is read via `await request.body()` **before** any parsing,
  exactly mirroring `app/api/webhooks.py`'s Shopify handler (which itself
  mirrors the requirement that Stripe/Shopify sign raw bytes, not parsed
  JSON).
- Signature verification via `stripe.Webhook.construct_event(...)`;
  `SignatureVerificationError` → 401, `ValueError` (malformed payload) →
  422. No unsigned/unverified code path exists.
- Idempotency uses a DB-level UNIQUE constraint on
  `BillingEvent.stripe_event_id`, inserted and `flush()`ed **before**
  any business-logic side effects run, with the resulting
  `IntegrityError` (not a prior `SELECT`) as the sole "already processed"
  signal — the same TOCTOU-safe pattern this codebase already learned it
  needed for `Transaction.external_order_id` (Batch 1's fix). If
  `handle_stripe_event` itself throws, the whole transaction
  (including the just-flushed `BillingEvent` row) is rolled back, so a
  failed delivery doesn't get permanently marked "already processed" —
  a real redelivery can still succeed later.

**Live probes** (`/tmp/webhook_probe.py`), hitting the *real*
(non-monkeypatched) Stripe signature verification path:

```
No Stripe-Signature header:        401 {"detail":"Invalid webhook signature"}
Garbage Stripe-Signature header:   401 {"detail":"Invalid webhook signature"}
Empty body:                        401 {"detail":"Invalid webhook signature"}
Malformed non-JSON body:           401 {"detail":"Invalid webhook signature"}
```

All fail closed with a clean 401 — no 500s, no crashes, no bypass.

`tests/test_billing.py` already has strong sequential coverage of this
exact surface (18 webhook-specific tests): signature rejection, per-event
handlers (`checkout.session.completed`, `subscription.created/updated/
deleted`, `invoice.payment_failed/paid`), same-event-id-twice idempotency,
unrecognized-event-type no-op, and — notably — explicit tests that a
hard-locked merchant can still reach every billing endpoint including the
webhook, login, `/auth/me`, and GDPR erase/export (the full exemption
list from §1 is directly tested here, which is why that list checks out
so cleanly above).

**Gap in what's tested:** the idempotency test only exercises *sequential*
redelivery (call once, then call again) — there's no thread-level
concurrent-redelivery test for the Stripe webhook the way
`test_earn_concurrency.py` has for the Shopify webhook/ledger. Given the
identical DB-unique-constraint-first pattern, I'd expect it to hold up
under real concurrency the same way (see §4 below, where I did run a real
concurrent-request race against the structurally identical winback
pattern and it held), but this specific path wasn't verified under true
concurrency by either the coder or me. **LOW** severity — flagging as a
test-coverage gap, not a demonstrated bug.

**Verdict: no idempotency or signature-verification bugs found.** This
is the most defensively-built surface in the batch.

---

## 4. Notification cooldown/dedup correctness under concurrency

**Design review** (`app/services/notifications.py::check_churn_escalations`):
uses the same atomic `UPDATE ... WHERE <not-already-transitioned> ...`
shape as `app/services/ledger.py`'s balance updates, checking
`result.rowcount == 1` as the "this request won the transition" signal,
rather than a Python-level read-then-write check. The
`.execution_options(synchronize_session=False)` is there because
SQLite round-trips `DateTime(timezone=True)` as tz-naive, which would
otherwise make SQLAlchemy's default "evaluate" sync strategy crash
comparing aware vs. naive datetimes in Python — **this is purely a
sync-strategy workaround, not a weakening of the atomicity guarantee**:
the actual `UPDATE...WHERE` still runs entirely DB-side, and the code
explicitly `db.get()`s + `db.expire()`s the winning member afterward so
downstream code (the notification body formatter) never reads stale
in-memory attributes. This is the correct application of the codebase's
established fix, not a regression of it.

**Live concurrency probe** (`/tmp/notif_race_probe.py`): real uvicorn
server (not the shared-session TestClient — same reasoning
`test_earn_concurrency.py` gives for why that matters), 15 threads behind
a `threading.Barrier` all hitting `GET /api/v1/ai/churn` at the same
instant for a merchant with one member freshly pushed into "high" risk
band, with `app.services.notifications.send_slack` monkeypatched to a
counter (same process as the live server thread, so the patch applies):

```
[SLACK SEND #1] '*1 member(s) just escalated to high churn risk*\n- Race Er'
status codes: [200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 200]
member churn band (sample): high 100.0

TOTAL SLACK SENDS ACROSS 15 CONCURRENT REQUESTS: 1
PASS: exactly one notification fired despite concurrent requests.
```

**Verdict: no duplicate-notification race found.** The atomic-UPDATE
pattern was applied correctly under real concurrent HTTP load, not just
in sequential unit tests. This is a clean pass on exactly the bug class
this codebase has been bitten by twice before.

---

## 5. Win-back auto_trigger default and point-cost logic

**`auto_trigger` default:** confirmed `False` in three independent
places — the DB column (`WinbackRule.auto_trigger: Mapped[bool] =
mapped_column(Boolean, default=False, nullable=False)`), the Pydantic
input schema (`WinbackRuleIn.auto_trigger: bool = False`), and the
router's "no rule saved yet" default response
(`app/api/winback.py::get_rule`). `PUT /winback/rule` is a full
upsert/replace (not a `PATCH`), and the frontend (`Winback.tsx`) always
submits the current value of all four fields on every save, so there's
no code path — frontend or API — where a merchant who never touched
win-back settings ends up with `auto_trigger=True`. Confirmed safe.

**Double-grant / re-offer protection:** `WinbackOffer.member_id` has a
DB-level `unique=True` constraint, and `_grant_and_record` wraps the
grant+record in a `db.begin_nested()` SAVEPOINT, catching the resulting
`IntegrityError` as "already offered, skip" — this is the same
constraint-first pattern verified in §3/§4, not a SELECT-then-branch
check. `tests/test_winback.py` has real coverage of this
(`test_manual_run_second_call_sends_zero_offers`,
`test_winback_offer_unique_constraint_rejects_duplicate_member_id`) but
— same gap as the Stripe webhook in §3 — those are **sequential** calls,
not a concurrency test. The task brief specifically asked me to verify
the coder's claimed test coverage actually proves what it claims; it
proves the DB constraint exists and that *sequential* re-runs are inert,
but not that concurrent runs are safe.

**Live concurrency probe** (`/tmp/winback_race_probe.py`): real uvicorn
server, 20 threads behind a `threading.Barrier`, all firing
`POST /api/v1/winback/run` at the same instant against one eligible
member (rule threshold 0.0, member pushed into churn eligibility):

```
status codes: [200 x20]
offers_sent per call: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]
total offers_sent summed across all calls: 1

Actual WinbackOffer rows in DB for this member: 1
PASS: exactly one WinbackOffer row despite 20 concurrent /winback/run calls.
```

**Verdict: no double-grant race found under real concurrency** — the
19 losing requests all correctly saw `offers_sent: 0` rather than
crashing or double-granting. This closes the gap in what the existing
tests actually exercised.

**Points/balance check:** re-confirmed live in the GDPR probe (§2) —
the granted `Redemption` has `points_spent=0`, `transaction_id=None`,
and does not touch `Member.points_balance` (matches
`grant_winback_reward`'s docstring and the plan's explicit
Batch-2-`mint_points=false`-style regression criterion). No bug found
here.

---

## 6. A/B experiment assignment stability and edge cases

All probes below run via `/tmp/experiments_probe.py` and
`/tmp/experiments_probe2.py` against a live TestClient.

**Assignment stability across refetch:** confirmed stable — two
consecutive `GET /experiments/{id}` calls return identical
`members_assigned_a`/`members_assigned_b` counts (`(6, 4) == (6, 4)`),
as expected from the deterministic SHA-256-hash assignment (no RNG
state, no re-assignment endpoint exists).

**Zero eligible members:** `POST /experiments` with 0 active members →
`201`, `members_assigned_a: 0, members_assigned_b: 0`, no crash.
`GET /results` on it → `200`, both variants report 0/0/0.0/0,
`z_score: null`, `directional_winner: "inconclusive"`. Clean.

**100/0 and 0/100 traffic splits:** `traffic_split=1.0` against 10
members → all 10 assigned to B, 0 to A. `traffic_split=0.0` → all 10 to
A, 0 to B. Both correct, no crash.

**Ending an experiment twice:** `POST /{id}/end` called twice in a row →
both return `200`, status stays `"completed"`, but **`ended_at` silently
advances to the second call's timestamp** (`ended_at changed on 2nd
call? True`). Not a crash and not really a "bug" in the sense of
producing wrong data, but it is a minor idempotency gap worth flagging:
the plan doesn't specify whether `ended_at` should be pinned to the
*first* end call, and a merchant/admin re-clicking "End experiment"
(e.g. after a slow response) would see the freeze-point silently move.
**LOW** severity, cosmetic.

**Missing traffic_split validation — the one real bug found here:**
`ExperimentCreate.traffic_split` has no `Field` bounds
(`app/schemas/experiments.py`), so out-of-range values are silently
*accepted* rather than rejected:

```
traffic_split=-0.5: 201 (creates successfully)
traffic_split=5.0:  201 (creates successfully)
```

Tracing the effect: `assign_variant`'s bucket check is
`bucket < traffic_split * 100` where `bucket` is `0..99`. A negative
split makes this condition unsatisfiable for every member (100%
silently goes to "a" instead of being rejected), and anything `>= 1.0`
makes it always true (100% silently goes to "b"). So the API doesn't
crash, but it silently produces a degenerate, fully-lopsided experiment
instead of a `422` telling the caller they made a mistake. The frontend
(`Experiments.tsx`) does constrain its slider to `[0.05, 0.95]`, so this
is not reachable through the shipped UI — but it's a genuine gap in the
API contract itself (reachable by anyone calling the API directly, and a
foot-gun for whoever builds the next UI on top of it). **MEDIUM-LOW**
severity: add `Field(ge=0.0, le=1.0)` to `traffic_split`.

**`recommend_for_member` exclusion logic can zero out a member's
recommendations — confirmed live, not hypothetical:**
`_excluded_reward_ids_for_member` filters out the *other* variant's
reward from a member's candidate pool while an experiment is running.
If a member's *own* assigned variant is also filtered out for an
unrelated reason (tier ineligibility, inactive), and the merchant's
reward catalog is small enough that there's no third option, the member
can end up with **zero** recommendations, which they would not have had
absent the experiment.

Live repro (`/tmp/experiments_probe2.py`): merchant with exactly two
rewards — "A-gold-only" (`tier_required="gold"`) and "B-any-tier" — 30
bronze members, one experiment covering both rewards as variants A/B:

```
assigned a/b: 13 17
13 of 30 bronze members now get ZERO recommendations
*** CONFIRMED: recommend_for_member can return an EMPTY list for a member
    who would otherwise have had a valid recommendation, purely because
    of experiment variant exclusion. ***
```

(Without the experiment running, these 13 bronze members assigned to
variant A would have seen variant B — "B-any-tier" — recommended to them
normally, since only the tier check would apply. With the experiment
running, B is filtered out for them because their *own* assigned variant
is A, even though A itself is tier-inaccessible to them.)

This is a real, demonstrable product gap for merchants with small reward
catalogs (which, per the plan's own framing, is exactly the expected
Ledgerly customer at MVP scale — a handful of rewards, not dozens). It's
not a crash and not a security issue, but it can make
`GET /ai/recommendations/{id}` silently go from "useful" to "empty" for
a meaningful fraction of a merchant's membership the moment they start
an A/B test, with no warning surfaced anywhere in the UI. **MEDIUM**
severity — recommend that `recommend_for_member` fall back to the
member's own assigned-variant reward even if it would otherwise be
filtered by tier/active checks in this specific "only option left"
case, or that `bulk_assign_members`/experiment creation warn when a
merchant's catalog is too small for safe exclusion.

---

## 7. Cross-tenant leakage

**Method:** live two-tenant probe (`/tmp/cross_tenant_probe.py`) — signed
up two independent merchants (A and B), had A create a reward, a member,
an experiment, and a win-back rule, then attempted every plausible IDOR
from B's own valid JWT.

```
B GET A's experiment:                404
B GET A's experiment results:        404
B POST end A's experiment:           404
B GET A's member:                    404
B GDPR-export A's member:            404
B GDPR-erase A's member:              404
B GET winback rule (own default, not A's enabled one): 200 {enabled: False, reward_id: None, ...}
B GET winback offers (own, empty, not A's):   200 []
B GET billing/subscription (own only):        200 {status: trialing, tier: None, ...}

B create experiment using A's reward ids:     400 "Both reward ids must belong to this merchant"
B set winback rule reward_id = A's reward:    400 "reward_id must belong to this merchant and be active"

B PATCH settings w/ merchant_id in request body (should be ignored): 200
  -> only affected B's own settings; merchant_id is derived purely from the JWT, body value ignored
```

**Verdict: no cross-tenant leakage found anywhere in Batch 3's new
surface.** Every new router (`billing.py`, `settings.py`, `winback.py`,
`experiments.py`, the GDPR endpoints in `members.py`) derives
`merchant_id` exclusively from the authenticated JWT via
`require_active_subscription`/`require_admin`/`get_current_user`, never
from a path/body/query parameter. Cross-merchant resource lookups
(experiment, member, reward-id ownership checks in win-back rule/
experiment creation) all correctly scope by `merchant_id` and return
`404`/`400` rather than leaking existence or data. This is a clean pass.

---

## 8. Schema-drift safety

**Static audit of every new column on an EXISTING table** (`Merchant`,
`Member`, `Redemption` — `TeamMember`/`Transaction`/`RewardCatalogItem`/
`FraudAlert` got no new columns this batch):

| Table | New column(s) this batch | `nullable` | Has default? |
|---|---|---|---|
| `merchants` | `stripe_customer_id`, `stripe_subscription_id`, `subscription_status`, `subscription_tier`, `subscription_current_period_end`, `trial_ends_at` | all `True` | n/a (nullable) |
| `merchants` | `notification_slack_webhook_url`, `notification_email`, `notify_on_churn_risk`, `notify_on_fraud_alert` | all `True` | n/a (nullable) |
| `members` | `erased_at`, `last_known_risk_band`, `risk_escalated_notified_at` | all `True` | n/a (nullable) |
| `redemptions` | `source` | `True` | Python-side `default="manual"` (does **not** apply to existing rows post-ALTER, correctly documented as such) |

**Every single new column on an existing table is `nullable=True`.**
None would fail Postgres's `ALTER TABLE ... ADD COLUMN` on a table that
already has rows (the exact failure mode that caused the real
`amount_usd`→`amount_gbp` incident this codebase already had). All five
new tables this batch (`BillingEvent`, `WinbackRule`, `WinbackOffer`,
`RewardExperiment`, `ExperimentAssignment`) are brand-new, so
`create_all` handles them with their full NOT NULL/FK constraints
correctly (no existing rows to violate) — confirmed by reading each
table definition in `app/db/models.py`.

**Live drift-simulation probe** (`/tmp/schema_drift_probe.py`) — the
most direct test of this, reproducing the actual incident class: hand-built
a "legacy" pre-Batch-3 schema (`merchants`/`members`/`redemptions` tables
missing every new column) with real pre-existing rows in each, then
booted `init_db()` against the *current* models:

```
Schema drift detected: adding missing column merchants.stripe_customer_id
... (14 columns total across merchants/members/redemptions) ...
init_db() SUCCEEDED -- no crash.

legacy merchant row after sync: ('Legacy Co', None, None)   <- subscription_status, notify_on_churn_risk both NULL, not crashed
legacy redemption row after sync (source should be NULL, not crash): (50, 'completed', None)

PASS: schema-drift sweep survived a legacy pre-Batch-3 table with existing rows.
```

All 14 new columns across the three touched tables were added via
`ALTER TABLE ... ADD COLUMN` against tables that already had rows, with
zero crashes, and the pre-existing rows now correctly read the new
columns back as `NULL` (exactly the "treat NULL as the intended default"
convention the plan calls for, and that
`wants_churn_notifications`/`wants_fraud_notifications` implement).

**Caveat:** this was run against SQLite only (matching this repo's local
dev/test setup — no Postgres instance available in this sandbox). The
`CreateColumn(column).compile(engine)` DDL-generation path is
dialect-aware and *should* render equivalently against Postgres, but a
real Postgres run (e.g. against a Railway staging DB before this batch
deploys) is the only way to be fully certain — flagging as residual risk
given this is the exact failure mode that has bitten this codebase
twice in production already. **LOW** severity given the static
nullable-everywhere audit above is unambiguous, but worth a real
Postgres smoke-test before deploy given the history.

**Verdict: no schema-drift violations found; design and implementation
both check out.**

---

## 9. Frontend build and basic sanity

```
$ npm run build   # runs "tsc -b && vite build"
✓ built in 4.66s
```

Clean build — zero TypeScript errors, zero Vite warnings. `tsc -b` (full
project type-check, stricter than `vite build` alone) passed with no
output at all, so there are no latent type errors hiding behind Vite's
more lenient transpile-only checking.

**Static review of the five new/touched files:**

- **`Billing.tsx`** — has `loading`/`error` states, `useEffect(load, [])`
  runs once on mount (no dependency-array bug, no infinite loop). Button
  states (`actingTier`, `openingPortal`) correctly disable themselves
  during in-flight requests. Fine.
- **`SubscriptionGate.tsx`** — the security-adjacent piece of this batch's
  frontend: correctly **fails open** on a subscription-status fetch
  error (`isAllowed = loadError || ...`) rather than accidentally
  locking a paying merchant out over a transient network blip — a
  deliberate, documented, and correct choice (this is a UX gate only;
  every real API call is still independently 402-gated server-side, so
  failing open here doesn't create a security hole). Its
  `ALLOWED_SUBSCRIPTION_STATUSES` set (`{"trialing", "active",
  "past_due"}`) is textually identical to the backend's
  `app/api/deps.py::ALLOWED_SUBSCRIPTION_STATUSES` — confirmed by
  reading both files side by side, no drift. Cleanup function in
  `useEffect` (`cancelled` flag) correctly guards against a
  setState-after-unmount warning. No infinite loops.
- **`Winback.tsx`** — loading/error/saving/running states all present
  and correctly gated (e.g. "Send win-back offers now" is disabled
  unless `rule?.enabled`). The rule-edit form always submits all four
  fields (`enabled`, `churn_risk_threshold`, `reward_id`,
  `auto_trigger`) on every save — avoids a footgun where saving one
  field could silently reset another to its schema default (relevant
  because `PUT /winback/rule` is a full replace, not a `PATCH`). No
  bugs found.
- **`Experiments.tsx`** — loading/error/creating/ending states present.
  The `traffic_split` slider is UI-clamped to `[0.05, 0.95]` — this is
  exactly the missing server-side validation gap identified in §6;
  confirms the backend bug is masked (not fixed) by this frontend, so a
  future second frontend or a direct API caller would still hit it.
  `variantARewardId === variantBRewardId` is checked client-side before
  allowing submission (matches the backend's own 400 check, defense in
  depth, no bug). No infinite loops.
- **`Settings.tsx`** — loading/error/saving states present, empty-state
  hint when neither Slack nor email is configured. No bugs found.

**Verdict: frontend is clean.** Build is green, and static review of all
five new/touched files found no unhandled error states, no missing
loading states, and no infinite-loop risks. The one finding relevant
here is confirmatory, not new: `Experiments.tsx`'s slider constraint is
the *only* thing currently preventing a user from hitting the
`traffic_split` validation gap from §6 through the shipped UI.

---

## Bugs found (severity-ranked)

### HIGH

1. **GDPR export omits real personal data that now exists (§2).**
   `MemberExportOut` (`app/schemas/gdpr.py`) does not include
   `winback_offers` or `experiment_assignments`, nor the new
   `Member.last_known_risk_band`/`risk_escalated_notified_at` fields,
   despite all of that data existing and being about a specific,
   identifiable member by the time this batch shipped. Live-verified: a
   member with a real win-back offer and a real experiment assignment
   exports with no trace of either. This is a genuine UK GDPR Art. 15/20
   compliance gap, not a hypothetical — the schema's own docstring is
   stale (says the tables "don't exist yet"; they do). **Fix before
   ship**: add the two list fields to `MemberExportOut`, populate from
   `WinbackOffer`/`ExperimentAssignment` filtered by `member_id`, same
   pattern as the three fields already there.

### MEDIUM

2. **`app/api/team.py` is not gated by `require_active_subscription`
   (§1).** A merchant with `subscription_status="canceled"` (confirmed
   live) can still invite and remove teammates indefinitely — every
   other product router correctly 402s. Not in `deps.py`'s documented
   exemption list, so this reads as a genuine oversight rather than a
   deliberate design choice, and the coder pass itself flagged it as
   unresolved. Impact is limited because gating is per-merchant, not
   per-seat (a newly invited teammate is immediately 402'd on every real
   feature too), and no seat-cap enforcement exists anywhere in this
   codebase regardless of gating — so this is an inconsistency to fix,
   not a revenue-bypass hole. **Fix**: swap `get_current_user`/
   `require_admin` for `require_active_subscription`/
   `require_admin_active_subscription` in `team.py`, matching
   `winback.py`/`experiments.py`'s convention.

3. **`recommend_for_member` can return zero recommendations solely
   because of A/B-experiment variant exclusion (§6).** Live-reproduced
   with a merchant running a 2-reward catalog as an A/B test: 13 of 30
   bronze members (assigned to the tier-gated variant) got an empty
   recommendation list, where they'd have gotten a valid one absent the
   experiment. Not a crash, but a real, silent product regression for
   merchants with small reward catalogs — which the plan itself frames
   as the expected Ledgerly customer profile. No warning surfaced
   anywhere. **Fix**: fall back to the member's own assigned-variant
   reward in this specific "nothing else eligible" case, or warn at
   experiment-creation time when the catalog is too small for safe
   exclusion.

### MEDIUM-LOW

4. **`ExperimentCreate.traffic_split` has no range validation (§6).**
   API accepts `traffic_split=-0.5` or `5.0` with `201`, silently
   producing a fully-lopsided (100/0) assignment instead of a `422`.
   Confirmed live. Only the frontend's slider (`[0.05, 0.95]`) currently
   prevents this from being reachable through the shipped UI — a direct
   API call or a future second frontend hits it immediately. **Fix**:
   `Field(ge=0.0, le=1.0)` on the schema.

### LOW

5. **`POST /experiments/{id}/end` is not idempotent on `ended_at`
   (§6).** Calling it twice moves `ended_at` forward to the second
   call's timestamp instead of pinning to the first. Cosmetic — status
   stays `"completed"` either way, no functional impact, plan doesn't
   specify expected behavior here.
6. **No thread-level concurrency test for the Stripe webhook's
   idempotency path (§3).** The sequential same-event-id-twice test
   exists and passes, and the underlying pattern (DB unique constraint
   inserted before side effects) is structurally identical to the
   winback pattern I did concurrency-test live and which held up — but
   the Stripe path itself wasn't verified under true concurrent
   redelivery, by either the coder or me. Recommend porting
   `test_earn_concurrency.py`'s barrier-based harness to this endpoint
   before relying on it in production.
7. **Missing `stripe` in the environment's pre-existing venv** — not a
   code bug (it's correctly listed in `requirements.txt`), just a
   one-line `pip install stripe` needed before the suite would import.
   Noted for completeness since it blocked the very first `pytest`
   collection attempt.

### Confirmed clean (no bugs found, verified live, not just read)

- **Test suite**: 196/196 pass, exactly as claimed (§0).
- **Billing route gating** everywhere except `team.py`: auth, billing,
  Shopify webhooks, GDPR erase/export are correctly never paywalled;
  every ordinary product route correctly hard-locks at 402 for
  cancelled/unpaid merchants (§1).
- **Stripe webhook** signature verification and idempotency — fails
  closed on missing/garbage/empty signatures, correct raw-body-first
  ordering, DB-constraint-based dedup (§3).
- **Notification dedup under real concurrency** — 15 concurrent
  requests, exactly 1 Slack send (§4).
- **Win-back `auto_trigger` default** and **double-grant protection
  under real concurrency** — 20 concurrent `/winback/run` calls,
  exactly 1 offer granted; points balance never touched by a comped
  reward (§5).
- **Experiment assignment stability, zero-member and 100/0-split edge
  cases, double-end doesn't crash** (§6).
- **Cross-tenant isolation** — every IDOR attempt against every new
  Batch 3 endpoint correctly 404s/400s; `merchant_id` is derived only
  from the JWT everywhere, including when a malicious body tries to
  inject one (§7).
- **Schema-drift safety** — every new column on an existing table is
  nullable; live-simulated the exact "legacy table with real rows"
  scenario that has bitten this codebase twice before, and the sweep
  survived cleanly (§8).
- **Frontend build** — clean `tsc -b && vite build`, no errors/warnings;
  all five new/touched pages have proper loading/error states and no
  infinite-loop risks (§9).

---

## Overall verdict

**Batch 3 is solid engineering with one real compliance gap that must
be fixed before this goes near production, and one real product
regression worth fixing before the A/B testing feature is exposed to
customers with small reward catalogs.**

The headline claim (196/196 tests passing) is true and was independently
re-verified end to end, not just trusted. More importantly, the batch's
own stated highest-risk item — the `get_current_merchant` →
`require_active_subscription` blast radius across nearly every router —
checks out almost perfectly under live adversarial testing (cross-tenant
IDOR, hard-lock/soft-lock boundaries, the full documented exemption
list), and the two previously-identified recurring bug classes in this
codebase's history (lost-update races and schema drift) were both
specifically hunted for and both came back clean under real concurrent
load and a live legacy-schema simulation, respectively — the coders
correctly re-applied the established atomic-UPDATE and
nullable-additive-column patterns rather than reintroducing either bug
class.

What the automated tests didn't catch, and what an adversarial pass
found instead:

1. The **GDPR export gap (HIGH)** is the one finding I'd block a release
   over. It's not a hypothetical or an edge case — it's the default,
   expected behavior today, live-verified, and it's exactly the kind of
   gap that turns into a real regulatory complaint for a product whose
   entire §1 was a GDPR compliance pass. The irony that the GDPR feature
   itself has a GDPR gap (introduced by scope/sequencing across coder
   passes, not carelessness within any single pass) is worth calling out
   explicitly to whoever reviews this.
2. The **`team.py` gating gap (MEDIUM)** and **experiment-exclusion
   zero-recommendations issue (MEDIUM)** are both real and should be
   fixed, but neither is a security or data-integrity emergency given
   the mitigating factors described above.
3. The **traffic_split validation gap (MEDIUM-LOW)** and the two **LOW**
   items are minor polish/coverage items, not blockers.

Everything else I went looking for — races, IDOR, signature bypasses,
schema-drift crashes, double-grants — I could not break. That's a
meaningfully stronger result than "the tests pass": I ran real
concurrent-HTTP-request races against the two areas explicitly built to
avoid this codebase's two historical bug classes, live-simulated the
exact legacy-schema scenario that has caused a real production incident
before, and tried real cross-tenant attacks against every new endpoint
— and none of it broke.

**Recommendation: fix the GDPR export gap (HIGH) before this ships to
any real merchant handling real member data. Fix the `team.py` gating
and experiment-exclusion issues (MEDIUM) before/shortly after. The
MEDIUM-LOW and LOW items can ride in a follow-up.** With the GDPR gap
closed, I'd sign off on this batch.
