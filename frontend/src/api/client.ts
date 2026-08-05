/**
 * Typed REST client for the Loyalty AI Framework backend.
 *
 * Endpoint shapes here are copied from the ACTUAL backend implementation
 * (backend/app/api/*.py + backend/app/schemas/*.py), not guessed from
 * PLAN.md. Base URL defaults to a relative "/api/v1" so it works via the
 * Vite dev-server proxy (see vite.config.ts) without any env config; set
 * VITE_API_BASE_URL to point at a different backend origin if needed.
 */

const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "/api/v1";

const TOKEN_STORAGE_KEY = "loyalty_ai_access_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function setToken(token: string | null): void {
  if (token) {
    localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    ...(options.body ? { "Content-Type": "application/json" } : {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...((options.headers as Record<string, string>) ?? {}),
  };

  const res = await fetch(`${API_BASE_URL}${path}`, { ...options, headers });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      // ignore — no JSON body
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }

  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------

export interface Token {
  access_token: string;
  token_type: string;
}

export interface MerchantOut {
  id: string;
  business_name: string;
  email: string;
}

export function login(email: string, password: string): Promise<Token> {
  return request<Token>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function signup(
  business_name: string,
  email: string,
  password: string
): Promise<MerchantOut> {
  return request<MerchantOut>("/auth/signup", {
    method: "POST",
    body: JSON.stringify({ business_name, email, password }),
  });
}

export function getMe(): Promise<MerchantOut> {
  return request<MerchantOut>("/auth/me");
}

// ---------------------------------------------------------------------
// Members
// ---------------------------------------------------------------------

export interface MemberWithChurn {
  id: string;
  first_name: string;
  last_name: string;
  email: string;
  points_balance: number;
  tier: string;
  is_active: boolean;
  joined_at: string;
  last_activity_at: string;
  erased_at: string | null;
  churn_risk_score: number | null;
  churn_risk_band: string | null;
}

export interface MemberCreate {
  first_name: string;
  last_name: string;
  email: string;
  tier?: string;
}

export function listMembers(includeChurn = true): Promise<MemberWithChurn[]> {
  return request<MemberWithChurn[]>(`/members?include_churn=${includeChurn}`);
}

export function getMember(memberId: string): Promise<MemberWithChurn> {
  return request<MemberWithChurn>(`/members/${memberId}`);
}

export function createMember(payload: MemberCreate): Promise<MemberWithChurn> {
  return request<MemberWithChurn>("/members", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------
// Transactions
// ---------------------------------------------------------------------

export interface TransactionOut {
  id: string;
  member_id: string;
  type: string;
  amount_gbp: number;
  points: number;
  channel: string;
  created_at: string;
}

export interface TransactionCreate {
  member_id: string;
  amount_gbp: number;
  channel?: string;
}

export function listTransactions(
  memberId?: string,
  limit = 100
): Promise<TransactionOut[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (memberId) params.set("member_id", memberId);
  return request<TransactionOut[]>(`/transactions?${params.toString()}`);
}

export function ingestTransaction(
  payload: TransactionCreate
): Promise<TransactionOut> {
  return request<TransactionOut>("/transactions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------
// Rewards
// ---------------------------------------------------------------------

export interface RewardOut {
  id: string;
  name: string;
  description: string;
  category: string;
  points_cost: number;
  tier_required: string;
  active: boolean;
}

export interface RewardCreate {
  name: string;
  description?: string;
  category?: string;
  points_cost: number;
  tier_required?: string;
}

export interface RedemptionOut {
  id: string;
  member_id: string;
  reward_id: string;
  points_spent: number;
  status: string;
}

export function listRewards(): Promise<RewardOut[]> {
  return request<RewardOut[]>("/rewards");
}

export function createReward(payload: RewardCreate): Promise<RewardOut> {
  return request<RewardOut>("/rewards", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function redeemReward(
  memberId: string,
  rewardId: string
): Promise<RedemptionOut> {
  return request<RedemptionOut>("/rewards/redeem", {
    method: "POST",
    body: JSON.stringify({ member_id: memberId, reward_id: rewardId }),
  });
}

// ---------------------------------------------------------------------
// AI layer
// ---------------------------------------------------------------------

export interface RecommendationOut {
  reward_id: string;
  reward_name: string;
  points_cost: number;
  score: number;
  reason: string;
}

export interface ChurnScoreOut {
  member_id: string;
  first_name: string;
  last_name: string;
  recency_days: number;
  frequency: number;
  monetary: number;
  churn_risk_score: number;
  risk_band: string;
}

export interface FraudAlertOut {
  id: string;
  transaction_id: string;
  member_id: string;
  reason: string;
  score: number;
  details: string;
  resolved: boolean;
  created_at: string;
}

export function getRecommendations(
  memberId: string,
  topN = 5
): Promise<RecommendationOut[]> {
  return request<RecommendationOut[]>(
    `/ai/recommendations/${memberId}?top_n=${topN}`
  );
}

export function getChurnScores(): Promise<ChurnScoreOut[]> {
  return request<ChurnScoreOut[]>("/ai/churn");
}

export function getMemberChurn(memberId: string): Promise<ChurnScoreOut> {
  return request<ChurnScoreOut>(`/ai/churn/${memberId}`);
}

export function getFraudAlerts(refresh = true): Promise<FraudAlertOut[]> {
  return request<FraudAlertOut[]>(`/ai/fraud-alerts?refresh=${refresh}`);
}

// ---------------------------------------------------------------------
// Insights (Batch 2): future value, next-best-product, CSV upload/export
// ---------------------------------------------------------------------

export interface FutureValueOut {
  member_id: string;
  first_name: string;
  last_name: string;
  horizon_days: number;
  predicted_future_value: number;
  model_used: "trained" | "heuristic";
  avg_order_value: number;
  monthly_purchase_rate: number;
}

export interface NextBestOut {
  category: string;
  product_name: string | null;
  score: number;
  reason: string;
  data_granularity: "product" | "category";
}

export interface InsightsUploadRowError {
  row: number;
  reason: string;
}

export interface InsightsUploadResult {
  rows_received: number;
  rows_ingested: number;
  rows_skipped_duplicate: number;
  rows_failed: number;
  members_created: number;
  errors: InsightsUploadRowError[];
}

export function getFutureValue(horizonDays = 90): Promise<FutureValueOut[]> {
  return request<FutureValueOut[]>(`/insights/future-value?horizon_days=${horizonDays}`);
}

export function getFutureValueForMember(
  memberId: string,
  horizonDays = 90
): Promise<FutureValueOut> {
  return request<FutureValueOut>(
    `/insights/future-value/${memberId}?horizon_days=${horizonDays}`
  );
}

export function getNextBestProduct(memberId: string, topN = 3): Promise<NextBestOut[]> {
  return request<NextBestOut[]>(`/insights/next-best-product/${memberId}?top_n=${topN}`);
}

// Not built on top of request() -- this is the first multipart/form-data
// call in this client (every other call sends a JSON body). FormData needs
// the browser to set its own `Content-Type: multipart/form-data;
// boundary=...` header, so we can't reuse request()'s
// always-set-Content-Type-to-json behavior here.
export async function uploadInsightsCsv(
  file: File,
  mintPoints = false
): Promise<InsightsUploadResult> {
  const token = getToken();
  const form = new FormData();
  form.append("file", file);

  const res = await fetch(
    `${API_BASE_URL}/insights/upload?mint_points=${mintPoints}`,
    {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    }
  );

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      // ignore -- no JSON body
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return (await res.json()) as InsightsUploadResult;
}

// ---------------------------------------------------------------------
// Billing (Batch 3 §2)
// ---------------------------------------------------------------------

export type SubscriptionTier = "starter" | "growth" | "scale";

export interface SubscriptionOut {
  subscription_status: string | null;
  subscription_tier: string | null;
  subscription_current_period_end: string | null;
  trial_ends_at: string | null;
}

export interface CheckoutSessionOut {
  checkout_url: string;
}

export interface PortalSessionOut {
  portal_url: string;
}

// Statuses that keep the dashboard/API fully usable -- mirrors
// backend/app/api/deps.py::ALLOWED_SUBSCRIPTION_STATUSES exactly (see
// PLAN_BATCH3.md §2). Anything else (canceled/unpaid/incomplete/
// incomplete_expired/null -- "never subscribed") is a hard lock.
export const ALLOWED_SUBSCRIPTION_STATUSES = new Set(["trialing", "active", "past_due"]);

export function getSubscription(): Promise<SubscriptionOut> {
  return request<SubscriptionOut>("/billing/subscription");
}

export function createCheckoutSession(tier: SubscriptionTier): Promise<CheckoutSessionOut> {
  return request<CheckoutSessionOut>("/billing/checkout-session", {
    method: "POST",
    body: JSON.stringify({ tier }),
  });
}

export function createPortalSession(): Promise<PortalSessionOut> {
  return request<PortalSessionOut>("/billing/portal-session", { method: "POST" });
}

// Usage-based pricing (replaces the earlier per-member-count tier caps --
// see backend/app/services/usage.py's module docstring). A "plan" is a
// flat monthly base fee that includes a number of insight runs (a CSV
// upload processed or a report exported) plus a per-run overage rate.
export interface PlanOut {
  tier: SubscriptionTier;
  name: string;
  base_price_gbp: number;
  included_runs: number;
  overage_price_gbp: number;
}

export interface UsageOut {
  period_start: string;
  tier: SubscriptionTier;
  plan_name: string;
  included_runs: number;
  insight_runs_used: number;
  overage_runs: number;
  estimated_overage_cost_gbp: number;
}

export function listPlans(): Promise<PlanOut[]> {
  return request<PlanOut[]>("/billing/plans");
}

export function getUsage(): Promise<UsageOut> {
  return request<UsageOut>("/billing/usage");
}

// ---------------------------------------------------------------------
// Notification settings (Batch 3 §3)
// ---------------------------------------------------------------------

export interface NotificationSettingsOut {
  notification_slack_webhook_url: string | null;
  notification_email: string | null;
  notify_on_churn_risk: boolean;
  notify_on_fraud_alert: boolean;
  notify_weekly_digest: boolean;
}

export interface NotificationSettingsUpdate {
  notification_slack_webhook_url?: string | null;
  notification_email?: string | null;
  notify_on_churn_risk?: boolean;
  notify_on_fraud_alert?: boolean;
  notify_weekly_digest?: boolean;
}

export function getNotificationSettings(): Promise<NotificationSettingsOut> {
  return request<NotificationSettingsOut>("/settings/notifications");
}

export function updateNotificationSettings(
  payload: NotificationSettingsUpdate
): Promise<NotificationSettingsOut> {
  return request<NotificationSettingsOut>("/settings/notifications", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------
// Weekly digest
// ---------------------------------------------------------------------

export interface DigestAtRiskMemberOut {
  member_id: string;
  name: string;
  recency_days: number;
}

export interface WeeklyDigestOut {
  generated_at: string;
  total_members: number;
  at_risk_count: number;
  at_risk_members: DigestAtRiskMemberOut[];
  predicted_value_90d: number;
  top_opportunity: string;
  headline: string;
}

export interface DigestSendResult {
  sent_via: string[];
  last_digest_sent_at: string;
}

export interface DigestStatusOut {
  enabled: boolean;
  last_digest_sent_at: string | null;
  has_notification_channel: boolean;
}

export function getDigestStatus(): Promise<DigestStatusOut> {
  return request<DigestStatusOut>("/digest/status");
}

export function previewDigest(): Promise<WeeklyDigestOut> {
  return request<WeeklyDigestOut>("/digest/preview");
}

export function sendDigestNow(): Promise<DigestSendResult> {
  return request<DigestSendResult>("/digest/send", { method: "POST" });
}

// ---------------------------------------------------------------------
// Onboarding: business-type picker. Feeds the AI layer's calibration
// fallback for merchants without enough transaction history yet to
// auto-calibrate (see backend/app/ai/churn_model.py).
// ---------------------------------------------------------------------

export interface BusinessTypeOption {
  value: string;
  label: string;
}

export interface BusinessProfileOut {
  business_type: string | null;
  calibration_source: string;
}

export function listBusinessTypes(): Promise<BusinessTypeOption[]> {
  return request<BusinessTypeOption[]>("/settings/business-types");
}

export function getBusinessProfile(): Promise<BusinessProfileOut> {
  return request<BusinessProfileOut>("/settings/business-profile");
}

export function updateBusinessProfile(businessType: string): Promise<BusinessProfileOut> {
  return request<BusinessProfileOut>("/settings/business-profile", {
    method: "PATCH",
    body: JSON.stringify({ business_type: businessType }),
  });
}

// Per-vertical sample data (app/services/sample_data.py) -- realistic
// starter members/transactions/rewards tailored to a business type, only
// ever generated for an account with zero real data (server-enforced,
// 409s otherwise).
export interface SampleDataOut {
  business_type: string;
  members_created: number;
  transactions_created: number;
  rewards_created: number;
}

export interface SampleDataStatusOut {
  is_sample_data: boolean;
}

export function loadSampleData(businessType: string): Promise<SampleDataOut> {
  return request<SampleDataOut>("/insights/sample-data", {
    method: "POST",
    body: JSON.stringify({ business_type: businessType }),
  });
}

export function getSampleDataStatus(): Promise<SampleDataStatusOut> {
  return request<SampleDataStatusOut>("/insights/sample-data/status");
}

// ---------------------------------------------------------------------
// GDPR / Compliance tab
// ---------------------------------------------------------------------

export interface MemberErasureResult {
  member_id: string;
  erased_at: string;
  already_erased: boolean;
}

// Loosely typed -- this is a full nested export payload (member,
// transactions, redemptions, fraud_alerts, experiment_assignments); the
// frontend only ever downloads it as a file, never reads individual
// fields, so there's no value in fully typing every nested shape here.
export type MemberExportOut = Record<string, unknown>;

export interface GdprAuditLogEntryOut {
  id: string;
  member_id: string;
  member_label: string;
  action: "export" | "erase";
  performed_by_email: string;
  created_at: string;
}

export interface GdprSummaryOut {
  total_members: number;
  erased_members: number;
  requests_last_30_days: number;
}

export function gdprExportMember(memberId: string): Promise<MemberExportOut> {
  return request<MemberExportOut>(`/members/${memberId}/gdpr-export`);
}

export function gdprEraseMember(memberId: string): Promise<MemberErasureResult> {
  return request<MemberErasureResult>(`/members/${memberId}/gdpr-erase`, { method: "POST" });
}

export function getGdprSummary(): Promise<GdprSummaryOut> {
  return request<GdprSummaryOut>("/gdpr/summary");
}

export function getGdprAuditLog(limit = 50): Promise<GdprAuditLogEntryOut[]> {
  return request<GdprAuditLogEntryOut[]>(`/gdpr/audit-log?limit=${limit}`);
}

// Clears business_type back to null so the onboarding picker (see
// components/OnboardingModal.tsx) replays on next dashboard load --
// lets you re-trigger the getting-started flow on an existing/demo
// account instead of only ever seeing it once on a brand-new signup.
export function resetBusinessProfile(): Promise<BusinessProfileOut> {
  return request<BusinessProfileOut>("/settings/business-profile/reset", { method: "POST" });
}

// ---------------------------------------------------------------------
// Note: the dedicated Win-back page/nav tab was removed -- at-risk
// members are now surfaced via the Members page's risk filter (churn
// risk data already comes back on MemberWithChurn, no separate fetch
// needed). The backend's GET/PUT /winback/rule + GET /winback/worklist
// endpoints still exist for programmatic use, but nothing in this
// frontend calls them anymore.
// ---------------------------------------------------------------------
// A/B testing for reward structures (Batch 3 §5)
// ---------------------------------------------------------------------

export interface ExperimentOut {
  id: string;
  merchant_id: string;
  name: string;
  variant_a_reward_id: string;
  variant_b_reward_id: string;
  traffic_split: number;
  status: string;
  started_at: string;
  ended_at: string | null;
}

export interface ExperimentDetailOut extends ExperimentOut {
  members_assigned_a: number;
  members_assigned_b: number;
}

export interface ExperimentCreate {
  name: string;
  variant_a_reward_id: string;
  variant_b_reward_id: string;
  traffic_split: number;
}

export interface VariantResultOut {
  variant: string;
  reward_id: string;
  reward_name: string;
  members_assigned: number;
  redemptions_count: number;
  redemption_rate: number;
  total_points_spent: number;
}

export interface ExperimentResultsOut {
  experiment_id: string;
  status: string;
  variant_a: VariantResultOut;
  variant_b: VariantResultOut;
  z_score: number | null;
  directional_winner: string;
  sample_size_caveat: string;
}

export function listExperiments(): Promise<ExperimentOut[]> {
  return request<ExperimentOut[]>("/experiments");
}

export function createExperiment(payload: ExperimentCreate): Promise<ExperimentDetailOut> {
  return request<ExperimentDetailOut>("/experiments", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getExperiment(id: string): Promise<ExperimentDetailOut> {
  return request<ExperimentDetailOut>(`/experiments/${id}`);
}

export function getExperimentResults(id: string): Promise<ExperimentResultsOut> {
  return request<ExperimentResultsOut>(`/experiments/${id}/results`);
}

export function endExperiment(id: string): Promise<ExperimentOut> {
  return request<ExperimentOut>(`/experiments/${id}/end`, { method: "POST" });
}

// report.csv needs the Authorization header, so it can't be a plain
// <a href> link (JWT isn't a cookie in this app) -- fetch as a blob and
// trigger a synthetic download instead.
export async function downloadInsightsReport(horizonDays = 90): Promise<void> {
  const token = getToken();
  const res = await fetch(
    `${API_BASE_URL}/insights/report.csv?horizon_days=${horizonDays}`,
    {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    }
  );
  if (!res.ok) {
    throw new ApiError(res.status, res.statusText);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "future_value_report.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
