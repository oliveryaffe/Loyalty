import React, { useEffect, useState } from "react";

import { isApiError } from "../AuthContext";
import {
  createCheckoutSession,
  createPortalSession,
  getSubscription,
  SubscriptionOut,
  SubscriptionTier,
} from "../api/client";
import { TIER_PLANS } from "../components/SubscriptionGate";
import { formatDateUK } from "../utils";

function StatusBadge({ status }: { status: string | null }) {
  if (!status) {
    return <span className="badge badge-high">no subscription</span>;
  }
  const variant = status === "active" || status === "trialing" ? "low" : status === "past_due" ? "medium" : "high";
  return <span className={`badge badge-${variant}`}>{status}</span>;
}

export default function Billing() {
  const [subscription, setSubscription] = useState<SubscriptionOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actingTier, setActingTier] = useState<SubscriptionTier | null>(null);
  const [openingPortal, setOpeningPortal] = useState(false);

  function load() {
    setLoading(true);
    getSubscription()
      .then(setSubscription)
      .catch((err) => setError(isApiError(err) ? err.message : "Unable to load subscription status."))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  function subscribe(tier: SubscriptionTier) {
    setActingTier(tier);
    setError(null);
    createCheckoutSession(tier)
      .then((r) => {
        window.location.href = r.checkout_url;
      })
      .catch((err) => {
        setError(isApiError(err) ? err.message : "Unable to start checkout.");
        setActingTier(null);
      });
  }

  function manageBilling() {
    setOpeningPortal(true);
    setError(null);
    createPortalSession()
      .then((r) => {
        window.location.href = r.portal_url;
      })
      .catch((err) => {
        setError(isApiError(err) ? err.message : "Unable to open the billing portal.");
        setOpeningPortal(false);
      });
  }

  return (
    <div>
      <h2>Billing</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8 }}>
        Manage your Ledgerly subscription plan and payment details.
      </p>
      {error && <p className="error-text">{error}</p>}

      {loading ? (
        <p className="loading">Loading billing status...</p>
      ) : (
        <>
          <div className="card-grid" style={{ marginBottom: 24 }}>
            <div className="card">
              <div className="label">Status</div>
              <div className="value" style={{ fontSize: 18 }}>
                <StatusBadge status={subscription?.subscription_status ?? null} />
              </div>
            </div>
            <div className="card">
              <div className="label">Plan</div>
              <div className="value" style={{ fontSize: 18, textTransform: "capitalize" }}>
                {subscription?.subscription_tier ?? "—"}
              </div>
            </div>
            <div className="card">
              <div className="label">Renews / ends</div>
              <div className="value" style={{ fontSize: 18 }}>
                {subscription?.subscription_current_period_end
                  ? formatDateUK(subscription.subscription_current_period_end)
                  : "—"}
              </div>
            </div>
            <div className="card">
              <div className="label">Trial ends</div>
              <div className="value" style={{ fontSize: 18 }}>
                {subscription?.trial_ends_at ? formatDateUK(subscription.trial_ends_at) : "—"}
              </div>
            </div>
          </div>

          <button className="secondary" onClick={manageBilling} disabled={openingPortal}>
            {openingPortal ? "Opening..." : "Manage billing (update card, cancel, invoices)"}
          </button>

          <h3 style={{ marginTop: 32 }}>Plans</h3>
          <div className="tier-grid">
            {TIER_PLANS.map((t) => {
              const isCurrent = subscription?.subscription_tier === t.tier;
              return (
                <div className="tier-card" key={t.tier}>
                  <div className="tier-name">{t.name}</div>
                  <div className="tier-price">{t.price}</div>
                  <div className="tier-cap">{t.cap}</div>
                  <button
                    className={isCurrent ? "secondary" : "primary"}
                    disabled={actingTier !== null || isCurrent}
                    onClick={() => subscribe(t.tier)}
                  >
                    {isCurrent ? "Current plan" : actingTier === t.tier ? "Redirecting..." : "Subscribe"}
                  </button>
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
