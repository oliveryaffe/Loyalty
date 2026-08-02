import React from "react";
import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../AuthContext";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/members", label: "Members" },
  { to: "/rewards", label: "Rewards" },
  { to: "/fraud-alerts", label: "Fraud Alerts" },
];

export default function Layout() {
  const { merchant, logout } = useAuth();

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <h1>
          Loyalty AI
          <br />
          {merchant?.business_name ?? "Merchant Dashboard"}
        </h1>
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
