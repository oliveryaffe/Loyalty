# PLAN_BATCH3.md — Ledgerly, Feature Batch 3

**Scope:** five additions on top of the shipped MVP + Batch 1 + Batch 2 (see
`README.md`, `PLAN_BATCH1.md`, `PLAN_BATCH2.md`) — **notifications**
(Slack/email on churn escalation + new fraud alerts), **win-back
campaigns** (rule-driven auto-offer for high-churn-risk members),
**Stripe billing** (subscription tiers + gating), **A/B testing** for
reward structures, and a **GDPR technical pass** (member erasure/export,
self-hosted fonts, placeholder legal pages). The GDPR legal *wording* and
the Railway EU-region data-residency migration are explicitly **out of
scope** for this document — a solicitor handles the former, a separate
infra task handles the latter (noted here only so nobody re-solves it).

**Baseline being extended:** FastAPI + SQLAlchemy 2.0 (SQLite locally,
Postgres in prod, no Alembic — schema drift is handled by
`app/db/base.py`'s `_apply_column_renames`/`_sync_missing_columns` sweep,
see "Migration approach" below), JWT auth via `TeamMember`/
`get_current_merchant`/`require_admin` (`app/api/deps.py`), points ledger
with atomic-UPDATE concurrency safety (`app/services/ledger.py`), three
in-process AI modules plus `future_value`/`next_best_product`
(`app/ai/*`), an `insights` CSV-upload surface (`app/api/insights.py`),
620 seeded members / ~7,200 GBP-denominated transactions
(`backend/scripts/seed_data.py`), 114 passing pytest tests
(`backend/tests/`), a React+TS+Vite dashboard (`frontend/src/`), and a
static-HTML marketing site (`marketing/index.html`, served by a bare
`python -m http.server` in `marketing/Dockerfile`).

---

## 0. Priority tiers and suggested build order

| Tier | Feature | Why |
|---|---|---|
| **P0** | §1 GDPR technical pass | Real, already-identified legal exposure (no erasure/export path is a live UK GDPR gap; the Google Fonts IP leak is a specific, court-tested issue). Also almost entirely self-contained/low-risk to implement — good first slice. |
| **P0** | §2 Stripe billing | Ledgerly cannot invoice a single customer without this — it blocks revenue outright, which makes it more urgent than any product feature below, even though it has the longest external lead time (owner must create a Stripe account). Start this **in parallel** with §1, not after it, specifically because of that lead time (see "External dependencies," end of doc). |
| **P1** | §3 Notifications | Builds directly on the churn/fraud AI that already shipped (Batches 1–2) — turns a passive dashboard signal into a proactive alert, and the "detect an escalation" plumbing it introduces is reused by win-back. |
| **P1** | §4 Win-back campaigns | Directly monetizes the churn-risk score (the product's stated differentiator) by turning it into an action, not just a number. Depends on §3's escalation-detection helper — build after it. |
| **P2** | §5 A/B testing | Valuable for larger/more mature merchants (naturally a Growth/Scale-tier feature, see §2's tier design) but the least urgent of the five and the smallest addressable audience at current merchant scale. Build last. |

**Suggested sequencing:** §1 and §2 in parallel (independent of each
other) → §3 → §4 (depends on §3) → §5 (independent, but lowest priority).

---

## Migration approach (applies to every feature below — read first)

Per the architect brief: **no new column or table in this batch may rely
on `create_all` alone; every new column on an *existing* table must be
safe under `app/db/base.py::_sync_missing_columns`.** Concretely, that
means:

- **Brand-new tables** (`WinbackRule`, `WinbackOffer`, `RewardExperiment`,
  `ExperimentAssignment`, `BillingEvent` — all introduced below) are
  zero-risk: `Base.metadata.create_all()` already handles missing tables
  correctly, including whatever `NOT NULL`/`FOREIGN KEY` constraints they
  need, because there are no existing rows to violate.
- **New columns on existing tables** (`Merchant`, `Member`, `Redemption`)
  **must be `nullable=True`**, exactly like every additive column so far
  (`Transaction.external_order_id`, `.source`, `.product_category`, etc.).
  `_sync_missing_columns` emits a plain `ALTER TABLE ... ADD COLUMN`
  with no `server_default`; a `NOT NULL` column would make that statement
  fail against a production Postgres table that already has rows (the
  exact class of incident `_apply_column_renames`'s docstring already
  warns about). **This applies even to booleans/enums that conceptually
  "always have a default"** — e.g. `Merchant.notify_on_fraud_alert` is
  declared `nullable=True` with a Python-side `default=True` (applies to
  *new* rows created via the ORM) but **existing merchant rows will read
  back as `NULL`** after the ALTER runs. Application code must treat
  `NULL` the same as the intended default — see the explicit helper
  functions specified in §3.
- No column is renamed or dropped in this batch, so `_apply_column_renames`
  needs no new entries.
- **New enum-like `str` columns** (e.g. `Member.last_known_risk_band`,
  `Merchant.subscription_status`) are plain `String`, not `SAEnum`,
  matching this codebase's existing convention (`Transaction.type`,
  `Redemption.status` are also plain strings, not DB-level enums) — keeps
  `_sync_missing_columns`'s generic `ADD COLUMN` path working without any
  enum-type special-casing.

Every "Data model" subsection below is written to satisfy this rule; the
coder should not need to touch `app/db/base.py` at all this batch (no new
renames, no new special-casing needed).

---

## 1. GDPR technical pass

*(Legal wording is explicitly out of scope — every string below is a
structural placeholder, not drafted legal language. A solicitor must
review before any of this is presented to a real end user.)*

### 1a. Member data erasure

**Decision: anonymize, not hard-delete.** Tradeoff, stated explicitly:

- `Member` has `cascade="all, delete-orphan"` relationships to both
  `Transaction` and `Redemption` (`app/db/models.py`). A literal
  `db.delete(member)` would cascade-delete every transaction and
  redemption that member ever generated. That silently corrupts the
  merchant's own business records — aggregate revenue figures, the
  `future_value`/churn training sets (Batch 2), and fraud-alert history
  all lose data points every time *any* member is erased, and the loss
  compounds over time as erasure requests accumulate. `FraudAlert.member_id`
  and `Redemption.member_id` are also both `nullable=False`, so a true
  hard-delete would need either a second cascade (deleting fraud-alert
  history too) or a schema change to make those FKs nullable — more
  invasive than this batch should be for a compliance fix.
- **Anonymization** (my choice): overwrite `first_name`, `last_name`,
  `email` with non-identifying placeholders (`"Erased"`, `"Member"`,
  `f"erased-{member.id}@deleted.ledgerly.invalid"`), set
  `is_active = False` (no further earn/redeem activity), and stamp a new
  `Member.erased_at` timestamp. The row and every FK'd `Transaction`/
  `Redemption`/`FraudAlert` stay intact — reports, AI training data, and
  fraud history all keep working exactly as before, attributed to a
  stable but now-anonymous id. This satisfies UK GDPR's erasure right
  (personal data — the name/email — is genuinely destroyed and
  irrecoverable) without destroying anonymized/aggregate business
  records, which fall outside personal-data scope once truly
  de-identified (UK GDPR Recital 26).
- **Assumption (flag for owner/solicitor):** `points_balance` and the
  transaction/redemption amounts themselves are left untouched — they're
  pseudonymous ledger data attached to an anonymous id at that point, not
  personal data. If legal review disagrees (e.g. wants balances zeroed
  too, or a defined retention/purge schedule for the *anonymized* rows
  after N years), that's a follow-up, not blocking this batch.
- **Idempotent:** calling erase twice is a no-op (checks `erased_at is
  not None` first, returns the current state rather than erroring).

**Data model:** `Member.erased_at: Mapped[datetime | None]` (nullable,
additive — safe under `_sync_missing_columns`).

**Endpoint:** `POST /api/v1/members/{member_id}/gdpr-erase` — added to
`app/api/members.py`. Gated with `require_admin`, not the looser
`get_current_merchant` every other member endpoint uses today
(`create_member`/`list_members`/`get_member` have no role gate).
**Assumption/judgment call:** deliberately stricter than the existing
member endpoints because this action is compliance-sensitive and
irreversible — flagged inline in case the owner wants erasure open to any
team member instead. Logs an audit line (`logger.info("GDPR erasure: "
"admin=%s member=%s", current_user.id, member.id)`) — no dedicated audit
table in scope; if legal review demands a durable audit trail, that's a
small follow-up (a table almost identical to `BillingEvent`, §2).
Response: `MemberErasureResult { member_id, erased_at, already_erased:
bool }`.

### 1b. Member data export (portability + subject access)

**Assumption (flag for owner):** UK GDPR technically separates Art. 15
(subject access, can be broad) from Art. 20 (portability, narrower —
machine-readable, only data provided by/observed about the subject). This
batch ships **one combined machine-readable JSON export** covering both,
which is common practice for an MVP-stage product but is a legal framing
call a solicitor should confirm, not something I'm asserting as
definitively compliant.

**Endpoint:** `GET /api/v1/members/{member_id}/gdpr-export` — same file,
`require_admin`-gated (same rationale as erasure: full-PII export is
sensitive enough to warrant the stricter gate, flagged the same way).
Returns `MemberExportOut`:

```python
class MemberExportOut(BaseModel):
    member: MemberOut
    transactions: list[TransactionOut]
    redemptions: list[RedemptionOut]
    fraud_alerts: list[FraudAlertOut]
    winback_offers: list[WinbackOfferOut]        # empty list if §4 not yet live
    experiment_assignments: list[ExperimentAssignmentOut]  # empty list if §5 not yet live
    exported_at: datetime
```

Returns `404` if the member doesn't exist / belongs to a different
merchant (same pattern as every other member-scoped lookup in this
codebase) and `410 Gone` if `member.erased_at is not None` (nothing
personal left to export — the anonymized row would otherwise round-trip
as a confusing near-empty export).

**New schema file:** `app/schemas/gdpr.py` (`MemberErasureResult`,
`MemberExportOut`).

### 1c. Self-hosted fonts (fixes the Google Fonts IP-leak issue)

Confirmed present in both places today:
- `frontend/index.html` (lines 7–10) and `marketing/index.html`
  (lines 8–10): both load `fonts.googleapis.com`/`fonts.gstatic.com` for
  Inter, weights 400/500/600/700/800.
- Inter is SIL Open Font License 1.1 — free to redistribute/self-host, no
  external account or licensing approval needed (unlike Stripe/SMTP
  below).

**Frontend dashboard (`frontend/`, Vite-built):**
- `npm install @fontsource/inter` (a pre-packaged, npm-installable
  self-hosted distribution of Inter — Vite bundles/hashes/caches the
  `.woff2` files at build time, so there's no manual binary-file
  management and no runtime network call to any third party).
- Remove the three Google Fonts `<link>` tags from `frontend/index.html`.
- In `frontend/src/main.tsx` (or `index.css`), import the five weights
  actually used: `import '@fontsource/inter/400.css'`, `/500.css`,
  `/600.css`, `/700.css`, `/800.css`.
- `frontend/src/index.css` line 3's `font-family: 'Inter', ...` stack
  needs no change — same font name, different loading mechanism.

**Marketing site (`marketing/`, plain static HTML, no build step, no
npm):** `@fontsource` isn't usable here since there's no bundler.
- Coder manually vendors the same 5 weights as `.woff2` files (e.g. via
  Inter's official GitHub releases, or the `google-webfonts-helper` tool)
  into a new `marketing/fonts/` directory.
- Remove the Google Fonts `<link>` tags from `marketing/index.html`;
  replace with an inline `@font-face` block (same `<style>` tag already
  in the file) pointing at `./fonts/Inter-Regular.woff2` etc., each with
  `font-display: swap` to preserve the current loading UX.
- **`marketing/Dockerfile` must change** — it currently does
  `COPY index.html .` only (line 4). Needs `COPY fonts/ ./fonts/` added,
  or the whole context copied (`COPY . .`), or the new font files never
  reach the deployed container and every page 404s on the font requests
  (silently falls back to system fonts — not broken, but defeats the
  point). **Flagging this explicitly because it's the kind of one-line
  Dockerfile omission that's easy to miss and only shows up in
  production**, the same shape of bug as Batch 1's CRITICAL-1
  (`seed_data.py --seed-if-empty` never actually wired into the
  Dockerfile CMD — see README.md §6).

**Assumption:** the Tailwind CDN `<script src="https://cdn.tailwindcss.com">`
tag also present in `marketing/index.html` (line 11) makes a similar
third-party request but is **out of scope** here — the task explicitly
scoped this GDPR pass to the fonts (the specific, court-tested issue),
not a general third-party-asset audit. Flagged here only so it isn't
mistaken for an oversight; a follow-up could self-host/pre-build Tailwind
too if the owner wants zero third-party requests site-wide.

### 1d. Placeholder legal pages

**Marketing site:** two new static pages, `marketing/privacy.html` and
`marketing/terms.html` (plain HTML, matching `index.html`'s existing
inline-Tailwind style so they look like part of the same site, not a
jarring unstyled drop-in). Both need `marketing/Dockerfile` updated the
same way as §1c (must actually be copied into the image). Footer links
added to `marketing/index.html` pointing at `/privacy.html` and
`/terms.html`.

Section skeleton for `privacy.html` (headings only — no clause text is
written here or should be written by the coder; each section body is a
single bracketed placeholder line):

```
1. Introduction [PLACEHOLDER — controller identity/contact details, to be drafted by a qualified UK solicitor]
2. What personal data we collect
3. Legal basis for processing (UK GDPR Art. 6)
4. How we use your data
5. Data retention
6. Your rights under UK GDPR (access, erasure, portability, objection, complaint to the ICO)
7. International data transfers
8. Data Processing Agreement — available on request (contact: [PLACEHOLDER email])
9. Contact / Data Protection contact
10. Changes to this policy
```

Section skeleton for `terms.html`:

```
1. Acceptance of terms
2. Description of service
3. Subscription & billing (see §2 of this plan — Stripe Checkout/Portal)
4. Acceptable use
5. Data processing (see Privacy Policy / DPA available on request)
6. Limitation of liability
7. Termination
8. Governing law (England & Wales)
9. Contact
```

Both pages must open with a **visible, unmissable banner** (not buried in
small print) reading approximately: *"This is a placeholder template, not
legal advice, and has not been reviewed by a solicitor. Do not treat this
page as Ledgerly's actual privacy/terms commitment until it has been
replaced with reviewed legal copy."* — matching the spirit of this
codebase's own honesty convention (`future_value.py`'s "deliberately not
overselling this" docstring) applied to legal content instead of ML
claims.

**Dashboard footer link:** `Layout.tsx` currently has no footer at all
(sidebar + `<main>` only, see lines 17–47). Add a small footer line below
the existing "Log out" button: plain `<a>` tags linking out to the
marketing site's `/privacy.html` / `/terms.html` (**not** duplicated
placeholder content inside the React app — one copy of the placeholder
text, not two that can drift out of sync). Needs the marketing site's
base URL, which isn't currently an env var anywhere in the frontend —
add `VITE_MARKETING_URL` (default `"/"` for local dev where they're not
co-hosted; production value set to the deployed marketing site's origin).

---

## 2. Stripe billing

**External dependency — flag prominently:** this feature needs a real
Stripe account before it can go live. **Development can proceed entirely
against Stripe test-mode keys**, which the owner (or the coder, on the
owner's behalf) can generate immediately from a free Stripe account with
no business verification required. Going *live* (real charges, real
payouts) requires Stripe's business verification (company details, bank
account), which only the account owner can complete — **this is a hard
blocker for production billing, not for building/testing this batch.**
Concretely needed before this ships to real customers: a Stripe account,
a secret key, a publishable key, a webhook signing secret, and three
Price IDs (one per tier, created in the Stripe dashboard).

### Pricing tiers (assumption — flag for owner validation)

**I have no access to the market-research findings referenced in the
task history (they weren't saved as a file in this repo), so the
figures below are my own reasonable-market-rate estimate for UK
SMB/mid-market retail loyalty SaaS, not a validated output of that
research. Treat every number here as a placeholder for the owner to
confirm or override, not a final price.**

| Tier | Price (ex. VAT) | Loyalty-member cap | Includes |
|---|---|---|---|
| **Starter** | £49/mo | up to 1,000 active members | Points ledger, reward catalog/redemption, churn risk + fraud detection AI, 2 team seats, email support |
| **Growth** | £149/mo | up to 10,000 active members | Everything in Starter + Shopify webhook ingestion, Insights (future value, next-best-product, CSV upload), Slack/email notifications, win-back automation, 5 team seats |
| **Scale** | £399/mo | unlimited members | Everything in Growth + A/B testing, unlimited team seats, priority support |

**Assumption:** 14-day free trial, **card required upfront**
(reduces low-quality trial signups, standard Stripe-recommended practice)
— `trial_period_days=14` on the Checkout Session. Flagged as an easy
override if the owner prefers no-card trials.

### Data model

```python
# Merchant — all nullable, additive
stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
subscription_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
# mirrors Stripe's own status strings: trialing/active/past_due/canceled/unpaid/incomplete/incomplete_expired
subscription_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)  # starter/growth/scale
subscription_current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

```python
class BillingEvent(Base):
    """Idempotency + audit log for Stripe webhook deliveries. Exactly the
    same lesson this codebase already learned twice (Transaction.external_order_id's
    unique constraint for Shopify webhook dedup) applied to Stripe: webhooks
    can be redelivered, so a DB-level UNIQUE constraint on the event id --
    not a SELECT-then-branch check -- is the actual source of truth."""
    __tablename__ = "billing_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    stripe_event_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    merchant_id: Mapped[str | None] = mapped_column(ForeignKey("merchants.id"), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    raw_payload: Mapped[str] = mapped_column(Text, default="")
```

New table, so `create_all` handles it directly — zero migration risk.

### API surface

New router `app/api/billing.py`, prefix `/api/v1/billing`:

| Endpoint | Method | Access | Purpose |
|---|---|---|---|
| `/billing/checkout-session` | `POST` | `require_admin` | Body `{tier: "starter"\|"growth"\|"scale"}`. Creates a Stripe Checkout Session (`mode="subscription"`, the tier's Price ID, `client_reference_id=merchant.id`, `customer_email`), returns `{checkout_url}` for the frontend to redirect to. |
| `/billing/portal-session` | `POST` | `require_admin` | Creates a Stripe Billing Portal session for the merchant's existing `stripe_customer_id` (card update, cancel, invoice history — all handled by Stripe's own hosted UI, no custom billing-management screens needed this batch). Returns `{portal_url}`. `404` if the merchant has no `stripe_customer_id` yet (hasn't subscribed once). |
| `/billing/subscription` | `GET` | `get_current_merchant` (any team member) | Current `subscription_status`, `subscription_tier`, `subscription_current_period_end`, `trial_ends_at` — powers the dashboard's billing banner/settings page. |
| `/billing/webhook` | `POST` | **none** (Stripe-signature-verified, not JWT) | Handles `checkout.session.completed`, `customer.subscription.created/updated/deleted`, `invoice.payment_failed`, `invoice.paid`. |

**`/billing/webhook` implementation note — mirrors `app/api/webhooks.py`
exactly:** must read the raw request body via `await request.body()`
*before* any parsing (Stripe signs raw bytes, same reason
`webhooks.py`'s own docstring gives for the Shopify path) and verify via
`stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)`.
On a `stripe.SignatureVerificationError`, `401`, exactly like the Shopify
HMAC failure path. Idempotency: insert a `BillingEvent` row keyed on
`stripe_event_id` first; catch the resulting `IntegrityError` as "already
processed, return 200 no-op" — same pattern as
`Transaction.external_order_id`'s unique constraint, not a
SELECT-then-branch check (that would be the same TOCTOU class of bug this
codebase already fixed twice for the ledger — see README.md §6
CRITICAL-2).

### Gating — what happens when a subscription lapses

Two explicit states, not a single "locked/unlocked" boolean:

- **`past_due` → SOFT lock.** Stripe is still auto-retrying the card
  (its own dunning schedule). Dashboard and API stay **fully
  functional** — a persistent, dismissible warning banner is shown
  (`GET /billing/subscription`'s response is what the frontend polls to
  decide whether to show it). Rationale: hard-blocking here would break
  the *merchant's own retail business* (their POS/Shopify store still
  wants to award points to real shoppers) over a card that might auto-recover
  in days — the cost of a false lock is higher than a few extra days of
  free access.
- **`canceled` / `unpaid` / `incomplete_expired` / no subscription ever
  started (trial expired without payment) → HARD lock.** All
  `/api/v1/*` endpoints that require `get_current_merchant` today are
  changed to require a new `require_active_subscription` dependency
  instead (thin wrapper: same as `get_current_merchant`, plus a
  `402 Payment Required` if the merchant is hard-locked). **Explicitly
  exempted from hard-lock** (kept on the plain `get_current_merchant`/no
  auth at all):
  - `app/api/auth.py` — must still be able to log in to *see* the lock
    screen and reach billing.
  - `app/api/billing.py` — must still be able to resubscribe.
  - `app/api/webhooks.py` (Shopify ingestion) — this is a third-party
    callback from the merchant's *own* live storefront; silently
    dropping it during a billing lapse would mean real purchase events
    (and the loyalty points a real shopper is owed) are lost forever,
    which is a worse outcome than letting ingestion keep working while
    the human dashboard is locked. When the merchant resubscribes,
    everything that accumulated is already there — nothing to
    reconcile.
  - The new §1 GDPR erasure/export endpoints — a compliance obligation
    doesn't pause because an invoice is unpaid.
- **Frontend:** a full-screen "Subscription required" interstitial
  (replaces the dashboard, not a toast) with a "Manage billing" button
  that calls `/billing/portal-session` — implemented as a check inside
  `RequireAuth` (`App.tsx`, currently only checks `isAuthenticated`) or a
  new wrapping component, gated on `GET /billing/subscription`'s status.

**New Settings fields (`app/config.py`):**
```python
stripe_secret_key: str | None = None
stripe_publishable_key: str | None = None
stripe_webhook_secret: str | None = None
stripe_price_id_starter: str | None = None
stripe_price_id_growth: str | None = None
stripe_price_id_scale: str | None = None
billing_success_url: str = "http://localhost:5173/billing/success"
billing_cancel_url: str = "http://localhost:5173/billing/cancel"
```
`requirements.txt`: add `stripe>=10,<12` (official SDK).

**Risk flag:** this feature touches the auth dependency used by nearly
every router in the app (`get_current_merchant` → `require_active_subscription`
swap). That's a wide blast radius for one change — the tester should
specifically verify that read-only browsing isn't accidentally broken for
`active`/`trialing`/`past_due` merchants (only `canceled`/`unpaid`/no-sub
should ever see a `402`), and that the exemption list above is complete
(a missed router would either wrongly lock out webhooks/GDPR endpoints,
or — worse — wrongly leave a paywalled feature accessible to a
non-paying merchant).

---

## 3. Notifications

### What triggers a notification

1. **A member's churn risk band newly escalates to `"high"`** — a
   *transition* (was `low`/`medium`, now `high`), not "is currently
   high" (that would re-fire on every dashboard refresh — see dedup
   below).
2. **A new `FraudAlert` row is created.** `run_fraud_detection()`
   (`app/ai/fraud_detector.py`) already dedupes internally — it skips any
   transaction that already has an alert (`existing_alert_txn_ids` check,
   lines 184–195) — so "a `FraudAlert` was just created by this call" is
   already exactly "genuinely new," no extra tracking table needed for
   fraud.

### Where the trigger lives (no scheduler exists — piggyback confirmed)

Checked `app/api/ai.py` and `app/main.py`: churn and fraud scores are
**entirely on-demand**, computed fresh on every request
(`score_all_members`/`score_member_churn` do zero DB writes;
`run_fraud_detection` writes only new `FraudAlert` rows). There is no
scheduler, no Celery, no APScheduler, nothing in `main.py`'s startup
beyond `init_db()`. Per the task brief's own steer, this batch
**piggybacks on the existing request-triggered recompute paths** rather
than introducing new infrastructure:
- Churn: hook into `GET /api/v1/ai/churn` (`app/api/ai.py::get_churn_scores`,
  the merchant-wide recompute call, hit whenever the Members page or a
  churn view loads).
- Fraud: hook into `GET /api/v1/ai/fraud-alerts?refresh=true`
  (`app/api/ai.py::get_fraud_alerts`, already the "recompute now" path,
  `refresh=True` by default).

**Known limitation, stated explicitly:** this means a notification only
fires when *someone* loads the relevant dashboard page — there is no
proactive "notify me overnight even though nobody logged in" behavior
this batch. The detection function is written schedule-agnostic on
purpose (a plain `(db, merchant) -> None` call, no request/response
coupling) specifically so that wiring a Railway Cron Job to a small new
`backend/scripts/run_scheduled_checks.py` later is an additive follow-up,
not a redesign — flagged as a natural next step, not attempted this
batch.

### Dedup / anti-spam design

**Data model additions:**
```python
# Member — both nullable, additive
last_known_risk_band: Mapped[str | None] = mapped_column(String(20), nullable=True)
risk_escalated_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

**Transition detection reuses this codebase's own established
concurrency pattern** — the exact atomic-`UPDATE ... WHERE` shape
`app/services/ledger.py` uses for balance changes (see its extensive
docstrings on why a read-then-write Python check is a TOCTOU race), rather than a naive
Python-level "if band == high and last != high" check-then-write:

```python
NOTIFICATION_COOLDOWN_HOURS = settings.notification_cooldown_hours  # default 24

cutoff = datetime.now(timezone.utc) - timedelta(hours=NOTIFICATION_COOLDOWN_HOURS)
result = db.execute(
    update(Member)
    .where(
        Member.id == member.id,
        or_(
            Member.last_known_risk_band.is_(None),
            Member.last_known_risk_band != "high",
            Member.risk_escalated_notified_at < cutoff,   # oscillation safety net
        ),
    )
    .values(last_known_risk_band="high", risk_escalated_notified_at=datetime.now(timezone.utc))
)
if result.rowcount == 1:
    escalated.append(member)  # this request "won" the transition -> notify
```
For members whose *current* band is not `"high"`, a plain (non-conditional)
update just keeps `last_known_risk_band` in sync so a future escalation is
correctly detected as a transition. This composes cleanly with a known,
low-severity residual risk: two concurrent requests racing in the exact
same instant could both see `rowcount == 1` for *different* members in
the same batch, but never for the *same* member (the `WHERE` clause
guarantees only one writer wins per row) — worst case is a member
notified twice within the same request race, not the repeated-spam
problem this design exists to prevent. Accepted as an MVP-scale tradeoff,
consistent with this codebase's existing posture (no full task-queue/lock
infrastructure exists anywhere yet).

**Fraud alerts need no extra dedup table** — `run_fraud_detection`'s
existing `existing_alert_txn_ids` check already *is* the dedup mechanism
(§ "What triggers a notification" above).

**Batching (the other half of anti-spam):** one notification message per
*triggering request*, not one per member/alert. A single churn-recompute
call that surfaces 12 newly-escalated members sends **one** Slack
message/email listing all 12 (capped at the first
`MAX_ITEMS_PER_NOTIFICATION = 10` with a "+N more" suffix for a genuinely
large burst) — never 12 separate messages.

### Per-merchant configuration (self-serve, no owner dependency)

```python
# Merchant — all nullable, additive
notification_slack_webhook_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
notification_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
notify_on_churn_risk: Mapped[bool | None] = mapped_column(Boolean, nullable=True)   # NULL treated as True, see below
notify_on_fraud_alert: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # NULL treated as True
```
**Explicit convention (per the "Migration approach" section above):**
because these are `nullable=True` booleans on an existing table, existing
merchant rows read back as `NULL` after the ALTER, not `True`. All
application code reads them through two small helpers rather than the
raw attribute:
```python
def wants_churn_notifications(merchant: Merchant) -> bool:
    return merchant.notify_on_churn_risk is not False  # NULL or True -> on
def wants_fraud_notifications(merchant: Merchant) -> bool:
    return merchant.notify_on_fraud_alert is not False
```

New router `app/api/settings.py`, prefix `/api/v1/settings`:

| Endpoint | Method | Access | Purpose |
|---|---|---|---|
| `/settings/notifications` | `GET` | `get_current_merchant` (any team member, read-only, matches `team.py`'s GET) | Current Slack URL/email/toggles |
| `/settings/notifications` | `PATCH` | `require_admin` | Update Slack webhook URL and/or notification email and/or toggles |

Slack webhook URL is validated as a well-formed `https://` URL (Pydantic
`HttpUrl`) but **not** verified against Slack's own URL shape
(`hooks.slack.com/...`) — kept loose deliberately in case a merchant uses
a compatible relay/proxy; the first real send attempt is the actual
verification (failures are logged, not surfaced as a hard error on save).

### Delivery mechanism

**No task queue exists in this codebase.** Rather than adding one (out of
proportion for this batch) or sending synchronously inside a hot GET
request (a slow/down Slack endpoint would directly slow down the
merchant's dashboard load — a real risk, flagged explicitly), delivery
uses **FastAPI's built-in `BackgroundTasks`** (already part of
Starlette/FastAPI, zero new dependency) — the HTTP response returns
immediately after the DB transition-detection above, and the actual
Slack POST / SMTP send happens after the response is sent.

New service `app/services/notifications.py`:
```python
def send_slack(webhook_url: str, text: str) -> None:
    """POSTs {"text": text} via httpx (already a dependency) with a short
    timeout (settings.notification_http_timeout_seconds, default 3s).
    Catches and logs (logger.warning) all exceptions -- a Slack outage
    must never surface as an error to the merchant, since this always
    runs as a background task after the response has already gone out."""

def send_email(to_address: str, subject: str, body: str) -> None:
    """smtplib against settings.smtp_host/port/username/password. No-ops
    with a logger.warning if smtp_host is unset (email sending "off" by
    default until the owner supplies SMTP credentials -- see External
    dependencies at the end of this doc)."""

def notify_merchant(merchant: Merchant, subject: str, body: str, background_tasks: BackgroundTasks) -> None:
    """Fans out to whichever of Slack/email the merchant configured, each
    as its own background task."""

def check_churn_escalations(db: Session, merchant: Merchant, results: list[ChurnResult]) -> list[Member]:
    """The atomic-UPDATE transition detection above. Returns members that
    just escalated to high risk *and* haven't been notified within the
    cooldown window. Pure DB-state function -- does not itself send
    anything; callers (ai.py, §4's winback service) decide what to do
    with the returned list."""
```
**Deliberately generic naming/placement:** `check_churn_escalations`
lives in `app/services/notifications.py` but is consumed by *both* this
feature and §4's win-back automation (which needs the exact same "who
just crossed into high risk this request" signal) — avoids duplicating
the transition-detection logic in two places. If this feels
mis-named once §4 is built, a trivial rename to
`app/services/churn_triggers.py` is a non-functional cleanup, not a
redesign.

**External dependency — flag clearly:** Slack webhook URLs are
**self-serve per merchant** (no owner dependency — each merchant
generates their own via Slack's "Incoming Webhooks" app, entirely their
side). **Email requires the owner to supply platform-level SMTP/transactional-email
credentials** (`smtp_host`/`smtp_username`/`smtp_password` in `app/config.py` —
e.g. an SES/Postmark/SendGrid SMTP relay, or a plain Gmail/O365 SMTP
account for a low-volume MVP). Without them, Slack notifications work
immediately (self-serve) but email notifications silently no-op (logged,
not user-facing broken) until the owner provisions something — same
"external dependency the owner must supply before go-live" category as
Stripe, just lower-stakes.

### API surface (recap) and acceptance criteria

1. `PATCH /settings/notifications` (admin) saves Slack URL + email +
   toggles; `GET /settings/notifications` (any team member) reflects
   them.
2. Seeding a member's transaction history to force a churn-band
   escalation, then calling `GET /api/v1/ai/churn` once with a Slack
   webhook configured → exactly one Slack POST fires (verifiable via a
   mocked `httpx` call in tests) listing that member.
3. Calling `GET /api/v1/ai/churn` again immediately after (no new
   escalations) → zero additional Slack POSTs, even though the member is
   still `"high"` band (the actual anti-spam regression test).
4. A member's band later drops to `"medium"` then re-escalates to
   `"high"` in a later request → a **second** notification fires (proves
   this is escalation-transition tracking, not a one-time-ever flag).
5. `GET /api/v1/ai/fraud-alerts?refresh=true` that produces N newly
   created `FraudAlert` rows → exactly one batched notification
   referencing all N (not N separate sends); a second call with no new
   alerts → zero sends.
6. `notify_on_fraud_alert=false` → fraud detection still runs/persists
   alerts as today, but zero notifications fire.
7. No Slack/email configured at all → detection/dedup logic still runs
   (state columns still update) but `notify_merchant` is a no-op — never
   an error, never blocks the response.
8. A forced-timeout/unreachable Slack URL does not delay or fail the
   triggering HTTP request (background task, verified via response-time
   assertion in a test).

---

## 4. Win-back campaigns

**Explicit MVP scope, per the task brief:** one rule per merchant (no
multi-rule campaign builder), a manual "send offers now" trigger, plus an
optional auto-trigger piggybacking on §3's escalation detection. Not a
campaign-builder UI.

### Data model

```python
class WinbackRule(Base):
    """One rule per merchant (unique constraint on merchant_id -- MVP
    simplicity, not a multi-rule campaign engine)."""
    __tablename__ = "winback_rules"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    churn_risk_threshold: Mapped[float] = mapped_column(Float, default=65.0, nullable=False)  # matches RISK_BAND_MEDIUM_MAX, i.e. "high" band by default
    reward_id: Mapped[str] = mapped_column(ForeignKey("reward_catalog_items.id"), nullable=False)
    auto_trigger: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # see below
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

class WinbackOffer(Base):
    """Audit trail + the actual anti-repeat-offer guard: unique on
    member_id means a member can receive at most ONE win-back offer,
    ever, for the lifetime of this MVP feature."""
    __tablename__ = "winback_offers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False, index=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id"), nullable=False, unique=True)
    rule_id: Mapped[str] = mapped_column(ForeignKey("winback_rules.id"), nullable=False)
    redemption_id: Mapped[str] = mapped_column(ForeignKey("redemptions.id"), nullable=False)
    churn_risk_score_at_trigger: Mapped[float] = mapped_column(Float, nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False)  # "manual" | "auto"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
```
Both brand-new tables — `create_all` handles them, zero migration risk.

```python
# Redemption — one nullable, additive column
source: Mapped[str] = mapped_column(String(20), default="manual", nullable=True)
```
**Assumption/judgment call — "never re-offer" is deliberately permanent,
not time-windowed.** A member who re-engages after a win-back offer and
later lapses again will *not* get a second offer under this design. This
is the simplest, safest MVP interpretation of "avoid re-offering
repeatedly," flagged explicitly as an easy override — a future version
could instead use a cooldown window (e.g. re-eligible after 180 days)
the same way §3's notification cooldown works, but that adds a
"did this member's risk genuinely reset in between" judgment call that
felt like scope creep for this batch.

### What "offering a reward" actually does

**Decision: auto-grant the reward for free, not credit points toward
it.** New function in a new `app/services/winback.py`:
```python
def grant_winback_reward(db: Session, member: Member, reward: RewardCatalogItem) -> Redemption:
    """Creates a completed Redemption with points_spent=0, source='winback'.
    Deliberately does NOT call redeem_reward() -- skips the balance check,
    tier check, and points debit entirely (a win-back offer exists
    specifically to re-engage members who might not otherwise qualify;
    gating it behind the same eligibility rules as a normal redemption
    would defeat the purpose). No Transaction row is created (no purchase
    event occurred and no points were spent -- differs deliberately from
    redeem_reward(), which always writes both a Redemption and a
    Transaction)."""
```
This intentionally bypasses `ledger.py`'s tier/balance checks — stated
explicitly since it's the one place this batch deviates from an
established validation path, and a reviewer should know that's on
purpose, not a missed check.

### Trigger paths

1. **Manual, admin-initiated:** `POST /api/v1/winback/run` — computes
   churn scores for all members (`score_all_members`, reused as-is, no
   AI-module changes), and for every member whose score
   `>= rule.churn_risk_threshold` **and** who has no existing
   `WinbackOffer` **and** `rule.enabled`, calls `grant_winback_reward`
   and records a `WinbackOffer(triggered_by="manual")`. Returns a summary
   (`offers_sent: int`, `member_ids: list[str]`).
2. **Automatic, piggybacking on §3's escalation detection:**
   `app/api/ai.py::get_churn_scores` — after calling §3's
   `check_churn_escalations(...)` (which returns members that *just*
   transitioned into `"high"` this request) — for each such member,
   *if* `rule.enabled and rule.auto_trigger`, also runs the same
   grant-and-record flow, tagged `triggered_by="auto"`.
   **`auto_trigger` defaults to `False`** — a deliberate,
   safety-first default: automatically giving away a free reward with no
   merchant review is a real financial commitment, and this codebase
   already has one explicit precedent for "the safe default is `false`"
   in exactly this spirit (`insights.upload`'s `mint_points=false`
   default, Batch 2 §2, flagged there as "the single highest-risk design
   choice... if a coder implements it differently"). Flagging the same
   reasoning here inline so it isn't silently flipped during
   implementation.

### API surface

New router `app/api/winback.py`, prefix `/api/v1/winback`, all
`require_admin` except the read-only `GET /rule`:

| Endpoint | Method | Access | Purpose |
|---|---|---|---|
| `/winback/rule` | `GET` | any team member | Current rule, or a default-disabled shape if none saved yet |
| `/winback/rule` | `PUT` | admin | Upsert the merchant's single rule. `400` if `reward_id` doesn't belong to this merchant or isn't `active` |
| `/winback/run` | `POST` | admin | Manual trigger (path 1 above) |
| `/winback/offers` | `GET` | any team member | History/audit list of `WinbackOffer` rows (who, when, which rule, manual vs auto) — also doubles as the "did this already happen" view for the merchant |

### Frontend

New page `frontend/src/pages/Winback.tsx`, route `/winback`, nav item
added to `Layout.tsx`'s `NAV_ITEMS`. Rule-editor form (reward dropdown
sourced from the existing `listRewards()` client call, threshold number
input defaulting to 65, enabled/auto-trigger toggles), a prominent "Send
win-back offers now" button (calls `POST /winback/run`, shows a result
summary toast — mirrors `Insights.tsx`'s upload-result-toast pattern),
and a table of past offers (`GET /winback/offers`).

### Acceptance criteria

1. `PUT /winback/rule` then `POST /winback/run` against seeded members
   with an artificially high churn score → exactly the eligible members
   (score ≥ threshold, no prior offer) get a `WinbackOffer` row and a
   completed, `source="winback"`, `points_spent=0` `Redemption`; their
   `points_balance` is **unchanged** (explicit regression check, same
   shape as Batch 2's `mint_points=false` acceptance criterion).
2. Running `POST /winback/run` a second time immediately after →
   `offers_sent: 0` (the unique constraint on `WinbackOffer.member_id`
   is the enforced guarantee, not just the eligibility query — a test
   should attempt to violate it directly and confirm the DB rejects it).
3. With `auto_trigger=true`, a `GET /api/v1/ai/churn` call that surfaces
   a fresh escalation to `"high"` for a member above threshold also
   produces a `WinbackOffer(triggered_by="auto")` with no manual call
   needed; with `auto_trigger=false` (the default), the same scenario
   produces zero offers until `/winback/run` is called manually.
4. `rule.enabled=false` → `POST /winback/run` returns `offers_sent: 0`
   regardless of eligible members (an explicit off-switch, verified).

---

## 5. A/B testing (reward structures)

**Honest framing, matching this codebase's existing convention
(`future_value.py`'s "not a production CLV model" framing applied here
to statistics):** at demo scale (620 seeded members split ~50/50), any
observed redemption-rate difference between variants will have wide
confidence intervals. This ships a **directional comparison, not a
rigorous significance test** — the results view says so explicitly
rather than presenting a z-score as if it were conclusive.

**Also worth being explicit about, since this app has no separate
member-facing storefront:** Ledgerly today is a merchant-admin dashboard
— `POST /rewards/redeem` is called by merchant staff, not by an end
shopper self-serving a reward through some consumer UI. So "assignment"
here is a **backend cohort split** (which arm a member is in) with two
concrete effects, not a literal two-versions-of-a-webpage split:
(1) `recommend_for_member` steers toward each member's assigned variant,
and (2) results are measured by comparing redemption behavior between
the two cohorts, whichever staff member actually processes the
redemption.

### Data model

```python
class RewardExperiment(Base):
    __tablename__ = "reward_experiments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    variant_a_reward_id: Mapped[str] = mapped_column(ForeignKey("reward_catalog_items.id"), nullable=False)
    variant_b_reward_id: Mapped[str] = mapped_column(ForeignKey("reward_catalog_items.id"), nullable=False)
    traffic_split: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)  # fraction assigned to B
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)  # running | completed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class ExperimentAssignment(Base):
    __tablename__ = "experiment_assignments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("reward_experiments.id"), nullable=False, index=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id"), nullable=False, index=True)
    variant: Mapped[str] = mapped_column(String(1), nullable=False)  # "a" | "b"
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (UniqueConstraint("experiment_id", "member_id"),)
```
Both brand-new tables — zero migration risk.

### Assignment mechanism

**Bulk, at experiment-creation time** (not lazy/on-exposure) — simplest
correct MVP interpretation of "simple random assignment." Deterministic,
not RNG-state-dependent: `int(hashlib.sha256(f"{experiment_id}:{member_id}".encode()).hexdigest(), 16) % 100 < traffic_split * 100`
decides the arm — reproducible without persisting any seed, and every
active member of the merchant at creation time gets an assignment in one
pass.

**Explicit MVP limitation, flagged:** members created *after* an
experiment starts are **not** automatically assigned (no hook in
`create_member`/CSV ingestion this batch) — they simply won't appear in
either arm and won't be steered by `recommend_for_member`. Acceptable for
an MVP where experiments are expected to run over the existing member
base; a follow-up could add assignment-on-creation if long-running
experiments become common.

### Effect on the product

`app/ai/recommender.py::recommend_for_member` gains one filtering step:
if the member has an `ExperimentAssignment` for a running experiment, and
the *other* variant's reward would otherwise appear in their
recommendations, it's filtered out (so a member only ever sees/is
steered toward their own assigned arm's version) — this is the concrete
behavioral lever the split has, in place of a consumer-facing UI split
that doesn't exist in this codebase.

### Results

`GET /api/v1/experiments/{id}/results` — per variant: `members_assigned`,
`redemptions_count` (via `Redemption.reward_id` matching that variant's
reward, joined against `ExperimentAssignment` so only *assigned* members'
redemptions count, not anyone else who happens to redeem that same
catalog item outside the experiment), `redemption_rate`,
`total_points_spent`. A simple two-proportion z-score is computed by hand
with `numpy` (no new dependency — `scipy` isn't already a dependency and
isn't worth adding for one calculation), surfaced as `z_score` and
`directional_winner: "a" | "b" | "inconclusive"` (inconclusive if
`|z| < 1.96`), with the response explicitly including a
`sample_size_caveat: str` field carrying the "small-sample, directional
only" framing described above — so the frontend can't accidentally
present it as more rigorous than it is.

### API surface

New router `app/api/experiments.py`, prefix `/api/v1/experiments`:

| Endpoint | Method | Access | Purpose |
|---|---|---|---|
| `/experiments` | `POST` | admin | Create + bulk-assign. Body: `name`, `variant_a_reward_id`, `variant_b_reward_id`, `traffic_split` (default 0.5). `400` if either reward doesn't belong to this merchant, is inactive, or the two reward ids are identical. |
| `/experiments` | `GET` | any team member | List with basic status |
| `/experiments/{id}` | `GET` | any team member | Detail incl. assignment counts |
| `/experiments/{id}/results` | `GET` | any team member | The comparison view above |
| `/experiments/{id}/end` | `POST` | admin | Sets `status="completed"`, `ended_at=now` — freezes the results framing as final; `recommend_for_member` stops steering once `status != "running"` |

### Frontend

New page `frontend/src/pages/Experiments.tsx`, route `/experiments`, nav
item in `Layout.tsx`. Create-experiment form (two reward dropdowns
sourced from `listRewards()`, name, split slider), experiment list with
status badges, and a results view (simple bar comparison of redemption
rate variant A vs. B, the `directional_winner`/caveat text rendered
prominently, not buried).

### Acceptance criteria

1. `POST /experiments` against the 620 seeded members → assignment count
   for A + B sums to 620 (every active member assigned exactly once),
   and the split is within a reasonable tolerance of `traffic_split`
   (e.g. 45–55% for a 0.5 split — deterministic hash-based, not
   perfectly 50/50, but should not be wildly skewed).
2. Re-running assignment for the same experiment is not possible (no
   duplicate-assignment endpoint exists) — `ExperimentAssignment`'s
   unique constraint is a defense-in-depth check, not the primary
   guard (the primary guard is simply that assignment only happens once,
   at creation).
3. `recommend_for_member` for a member in variant A never returns
   variant B's reward id (and vice versa) while the experiment is
   `"running"`, for a seeded member/experiment combination confirmed via
   test.
4. `GET /experiments/{id}/results` correctly attributes redemptions only
   to members who are actually assigned to that experiment (a control
   test: a non-assigned member redeeming the same catalog reward must
   not inflate either variant's count).
5. `POST /experiments/{id}/end` → subsequent `recommend_for_member` calls
   no longer filter by variant for that experiment's rewards.

---

## Cross-cutting summary: new config, new dependencies, risk to the existing 114 tests

### `app/config.py` additions (all optional/defaulted — zero risk to `test_config.py`)
```python
# Notifications (§3)
smtp_host: str | None = None
smtp_port: int = 587
smtp_username: str | None = None
smtp_password: str | None = None
smtp_from_address: str = "notifications@ledgerly.app"
notification_http_timeout_seconds: float = 3.0
notification_cooldown_hours: int = 24

# Stripe billing (§2)
stripe_secret_key: str | None = None
stripe_publishable_key: str | None = None
stripe_webhook_secret: str | None = None
stripe_price_id_starter: str | None = None
stripe_price_id_growth: str | None = None
stripe_price_id_scale: str | None = None
billing_success_url: str = "http://localhost:5173/billing/success"
billing_cancel_url: str = "http://localhost:5173/billing/cancel"
```

### `requirements.txt` additions
- `stripe>=10,<12` (§2)
- No new dependency for §3 (`httpx` already present, used by tests;
  `smtplib` is stdlib), §4 (pure SQLAlchemy), or §5 (`numpy` already
  present).

### `frontend/package.json` additions
- `@fontsource/inter` (§1c)

### Risk to the existing 114 tests

| Change | Existing test(s) at risk | Required fix |
|---|---|---|
| New nullable columns on `Merchant`/`Member`/`Redemption` | `test_auth.py`, `test_members.py`, `test_team.py`, `test_ledger.py`, `test_redemption_concurrency.py` — none construct these models positionally (grep confirms kwargs-only construction throughout, same as Batch 2's finding) | None expected |
| `get_current_merchant` → `require_active_subscription` swap on most routers (§2) | **Every** existing API test that authenticates as the seeded demo merchant | **Real risk, not zero-risk like the rest of this table.** The seed script's demo merchant needs a `subscription_status="active"` (or the dependency needs to treat `NULL` as "not yet subscribed" → hard-locked, which would break every single existing test that logs in as the demo merchant). **Explicit fix required:** `scripts/seed_data.py` must set the demo merchant's `subscription_status="active"` and a far-future `subscription_current_period_end` at seed time, and `backend/tests/conftest.py`'s test-merchant fixture(s) must do the same. Flagged here as the single highest-regression-risk item in this entire batch — get this wrong and the *entire* pre-existing suite fails, not just new tests. |
| New routers (`billing.py`, `settings.py`, `winback.py`, `experiments.py`) | None — purely additive | None |
| `recommend_for_member` gains a filtering step (§5) | `test_recommender.py` | Existing tests have no `ExperimentAssignment` rows, so the filter is a no-op for them by construction (nothing to filter) — should pass unmodified, but the tester should confirm explicitly rather than assume |
| `run_fraud_detection`'s caller in `ai.py` now also calls `check_churn_escalations`/notification code | `test_fraud_detector.py` (tests the pure `detect_fraud` function directly, not the route) should be unaffected; any existing route-level fraud test needs a "no Slack/email configured" seeded merchant to hit the no-op path cleanly, not error | Verify existing route tests still pass with the new code path being a no-op when unconfigured |

**Net:** every feature's *data model* change is additive/zero-risk by the
same reasoning Batch 1/2 already established. The one genuinely
consequential risk is §2's dependency swap — it's called out here and
inline in §2 specifically so it isn't discovered late by the tester.

---

## Consolidated assumptions / judgment calls (all also flagged inline above)

1. **§1a** — anonymize, not hard-delete, for member erasure (business/technical
   tradeoff; a solicitor may want a stricter position).
2. **§1a/§1b** — erasure and export both gated `require_admin`, stricter
   than the existing ungated member CRUD endpoints.
3. **§1b** — one combined JSON export covers both Art. 15 and Art. 20
   rather than two separate, narrower endpoints.
4. **§2** — pricing tiers/amounts are my own market estimate, not sourced
   from the (unavailable-to-me) prior research task; treat as placeholder
   numbers pending owner validation.
5. **§2** — 14-day trial, card required upfront.
6. **§2** — `past_due` = soft lock (fully functional + banner);
   `canceled`/`unpaid`/no-subscription = hard lock (402 on most routes,
   with an explicit exemption list: auth, billing, Shopify webhook
   ingestion, GDPR erasure/export).
7. **§3** — notification delivery via FastAPI `BackgroundTasks`, not a
   real task queue; a Slack/SMTP outage is caught/logged, never surfaced
   to the merchant.
8. **§3** — churn-escalation and fraud-alert checks only fire on existing
   on-demand recompute requests (no scheduler this batch); flagged as a
   known "nobody logged in = no notification" limitation, with the
   detection function written schedule-agnostic for an easy future add.
9. **§3** — `NULL` on the new nullable boolean toggle columns is treated
   as "on" (`is not False`), not "off" — required reading convention for
   any code touching `Merchant.notify_on_*`.
10. **§4** — win-back reward is granted for free (comped redemption,
    bypassing tier/balance checks), not credited as spendable points.
11. **§4** — `auto_trigger` defaults to `False` (no automatic free-reward
    giveaway without explicit merchant opt-in) — same reasoning precedent
    as Batch 2's `mint_points=false` default.
12. **§4** — a member can receive at most one win-back offer ever (no
    re-eligibility window).
13. **§5** — assignment is bulk/at-creation, not lazy/on-exposure; members
    joining after an experiment starts are not auto-assigned.
14. **§5** — results are explicitly framed as directional, not
    statistically rigorous, given demo-scale sample sizes.

## External dependencies the owner must supply before production go-live

- **Stripe account, secret key, publishable key, webhook signing secret,
  and 3 Price IDs (§2).** Development/testing can proceed fully against
  Stripe test-mode keys (free, no verification needed) — this is a
  blocker for *charging real money*, not for building/testing this
  batch.
- **SMTP/transactional-email credentials for the platform (§3), only if
  email notifications are wanted.** Slack notifications need no owner
  action (each merchant self-serves their own webhook URL). Until SMTP
  credentials are supplied, email sending silently no-ops (logged
  warning, not a broken feature) — Slack-only notifications work today
  with zero owner action.
- **Inter font files for the marketing site (§1c) need no external
  account** (SIL OFL, freely self-hostable) — noted here only to
  contrast with the two real dependencies above; do not block on this.
