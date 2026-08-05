import React from "react";
import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../AuthContext";
import OnboardingModal from "./OnboardingModal";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/members", label: "Members" },
  { to: "/rewards", label: "Rewards" },
  { to: "/fraud-alerts", label: "Fraud Alerts" },
  { to: "/insights", label: "Insights" },
  { to: "/settings", label: "Settings" },
  { to: "/billing", label: "Billing" },
  { to: "/compliance", label: "Compliance" },
  { to: "/locations", label: "Locations" },
];

// Base URL of the marketing site, which hosts the single canonical copy of
// the (placeholder) privacy/terms pages -- see PLAN_BATCH3.md §1d. Not
// duplicated inside the dashboard so there's only ever one copy of the
// legal placeholder text to keep in sync. Defaults to "/" for local dev,
// where the marketing site and dashboard aren't co-hosted at the same
// origin and this link is mostly a no-op; set VITE_MARKETING_URL to the
// deployed marketing site's origin in production.
const MARKETING_URL: string =
  (import.meta.env.VITE_MARKETING_URL as string | undefined) ?? "/";

export default function Layout() {
  const { merchant, logout } = useAuth();

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="brand-mark">L</div>
          <span className="brand-word">Ledgerly</span>
        </div>
        <p className="sidebar-subtitle">
          {merchant?.business_name ?? "Merchant Dashboard"}
        </p>
        <nav>
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => (isActive ? "active" : undefined)}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <button className="logout" onClick={logout}>
          Log out{merchant ? ` (${merchant.email})` : ""}
        </button>
        <div className="sidebar-footer">
          <a href={`${MARKETING_URL}privacy.html`} target="_blank" rel="noreferrer">
            Privacy
          </a>
          <a href={`${MARKETING_URL}terms.html`} target="_blank" rel="noreferrer">
            Terms
          </a>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
      <OnboardingModal />
    </div>
  );
}
