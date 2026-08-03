import React, { useEffect, useState } from "react";

import { isApiError } from "../AuthContext";
import {
  ALLOWED_SUBSCRIPTION_STATUSES,
  createCheckoutSession,
  createPortalSession,
  getSubscription,
  SubscriptionOut,
  SubscriptionTier,
} from "../api/client";

export const TIER_PLANS: { tier: SubscriptionTier; name: string; price: string; cap: string }[] = [
  { tier: "starter", name: "Starter", price: "£49/mo", cap: "up to 1,000 members" },
  { tier: "growth", name: "Growth", price: "£149/mo", cap: "up to 10,000 members" },
  { tier: "scale", name: "Scale", price: "£399/mo", cap: "unlimited members" },
];

/**
 * Wraps the authenticated dashboard (mounted from App.tsx's RequireAuth,
 * PLAN_BATCH3.md §2's "Frontend" note): fetches GET /billing/subscription
 * once per mount and renders one of three things --
 *
 *  - hard lock (canceled/unpaid/incomplete/incomplete_expired/no
 *    subscription ever): a full-screen "Subscription required"
 *    interstitial replacing the dashboard entirely, not a toast.
 *  - soft lock (past_due): the dashboard renders normally, plus a
 *    persistent, dismissible warning banner (Stripe is still auto-
 *    retrying the card -- nothing is actually broken yet).
 *  - active/trialing: dashboard renders normally, nothing shown.
 *
 * Fails OPEN on an error fetching the subscription itself (e.g. a
 * transient network hiccup) -- a broken status check must never be the
 * thing that locks a paying merchant out of their own dashboard.
 */
export function SubscriptionGate({ children }: { children: React.ReactNode }) {
  const [subscription, setSubscription] = useState<SubscriptionOut | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [bannerDismissed, setBannerDismissed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getSubscription()
      .then((sub) => {
        if (!cancelled) setSubscription(sub);
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) {
    return (
      <p className="loading" style={{ padding: 32 }}>
        Checking subscription status...
      </p>
    );
  }

  const status = subscription?.subscription_status ?? null;
  const isAllowed = loadError || (status !== null && ALLOWED_SUBSCRIPTION_STATUSES.has(status));

  if (!isAllowed) {
    return <SubscriptionRequiredScreen status={status} />;
  }

  return (
    <>
      {status === "past_due" && !bannerDismissed && (
        <PastDueBanner onDismiss={() => setBannerDismissed(true)} />
      )}
      {children}
    </>
  );
}

function PastDueBanner({ onDismiss }: { onDismiss: () => void }) {
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function manageBilling() {
    setOpening(true);
    setError(null);
    createPortalSession()
      .then((r) => {
        window.location.href = r.portal_url;
      })
      .catch((err) => {
        setError(isApiError(err) ? err.message : "Unable to open the billing portal.");
        setOpening(false);
      });
  }

  return (
    <div className="past-due-banner">
      <span>
        <strong>Your last payment failed.</strong> Stripe is automatically retrying your card --
        your account is fully functional in the meantime, but please update your billing details
        to avoid losing access.
        {error && <span className="error-text" style={{ marginLeft: 8 }}>{error}</span>}
      </span>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexShrink: 0 }}>
        <button className="secondary" onClick={manageBilling} disabled={opening}>
          {opening ? "Opening..." : "Update billing"}
        </button>
        <button className="banner-dismiss" onClick={onDismiss} aria-label="Dismiss">
          &times;
        </button>
      </div>
    </div>
  );
}

function SubscriptionRequiredScreen({ status }: { status: string | null }) {
  const [actingTier, setActingTier] = useState<SubscriptionTier | null>(null);
  const [openingPortal, setOpeningPortal] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
    <div className="subscription-lock">
      <div className="subscription-lock-card">
        <div className="sidebar-brand" style={{ marginBottom: 16, justifyContent: "center" }}>
          <div className="brand-mark">L</div>
          <span className="brand-word">Ledgerly</span>
        </div>
        <h1>Subscription required</h1>
        <p className="subtitle">
          {status
            ? `Your subscription is currently "${status}" -- choose a plan below to restore access.`
            : "Choose a plan below to activate your Ledgerly account."}
        </p>
        {error && <p className="error-text">{error}</p>}
        <div className="tier-grid">
          {TIER_PLANS.map((t) => (
            <div className="tier-card" key={t.tier}>
              <div className="tier-name">{t.name}</div>
              <div className="tier-price">{t.price}</div>
              <div className="tier-cap">{t.cap}</div>
              <button
                className="primary"
                disabled={actingTier !== null}
                onClick={() => subscribe(t.tier)}
              >
                {actingTier === t.tier ? "Redirecting..." : "Subscribe"}
              </button>
            </div>
          ))}
        </div>
        <button
          className="secondary"
          style={{ marginTop: 20, width: "100%" }}
          onClick={manageBilling}
          disabled={openingPortal}
        >
          {openingPortal ? "Opening..." : "Manage existing billing"}
        </button>
      </div>
    </div>
  );
}
