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
