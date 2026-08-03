import React from "react";
import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../AuthContext";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/members", label: "Members" },
  { to: "/rewards", label: "Rewards" },
  { to: "/fraud-alerts", label: "Fraud Alerts" },
  { to: "/insights", label: "Insights" },
];

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
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
