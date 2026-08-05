import React from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { AuthProvider, useAuth } from "./AuthContext";
import Layout from "./components/Layout";
import { SubscriptionGate } from "./components/SubscriptionGate";
import Billing from "./pages/Billing";
import Dashboard from "./pages/Dashboard";
import FraudAlerts from "./pages/FraudAlerts";
import Insights from "./pages/Insights";
import Login from "./pages/Login";
import Members from "./pages/Members";
import Rewards from "./pages/Rewards";
import Settings from "./pages/Settings";

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return <p className="loading" style={{ padding: 32 }}>Loading...</p>;
  }
  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  // PLAN_BATCH3.md §2: gates the whole dashboard on GET /billing/subscription's
  // status -- a full-screen "Subscription required" interstitial for a
  // hard-locked merchant (canceled/unpaid/no-subscription), a dismissible
  // warning banner for the past_due soft lock, and a no-op for
  // active/trialing. See components/SubscriptionGate.tsx.
  return <SubscriptionGate>{children}</SubscriptionGate>;
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<Dashboard />} />
        <Route path="members" element={<Members />} />
        <Route path="rewards" element={<Rewards />} />
        <Route path="fraud-alerts" element={<FraudAlerts />} />
        <Route path="insights" element={<Insights />} />
        <Route path="settings" element={<Settings />} />
        <Route path="billing" element={<Billing />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
}
