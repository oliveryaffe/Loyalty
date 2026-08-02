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
  amount_usd: number;
  points: number;
  channel: string;
  created_at: string;
}

export interface TransactionCreate {
  member_id: string;
  amount_usd: number;
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
