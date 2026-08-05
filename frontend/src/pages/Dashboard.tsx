import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  BenchmarkOut,
  BusinessProfileOut,
  BusinessTypeOption,
  FraudAlertOut,
  FutureValueOut,
  getBenchmark,
  getBusinessProfile,
  getFraudAlerts,
  getFutureValue,
  getSampleDataStatus,
  listBusinessTypes,
  listMembers,
  listTransactions,
  MemberWithChurn,
  TransactionOut,
} from "../api/client";
import { formatGBP, formatDateUK } from "../utils";

const JOURNEY_DISMISSED_KEY = "ledgerly_dashboard_journey_dismissed";

function OnboardingJourney({ onDismiss }: { onDismiss: () => void }) {
  return (
    <div className="card" style={{ marginBottom: 20, position: "relative", maxWidth: 680 }}>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss"
        style={{
          position: "absolute",
          top: 10,
          right: 10,
          background: "none",
          border: "none",
          color: "var(--text-secondary)",
          cursor: "pointer",
          fontSize: 18,
          lineHeight: 1,
        }}
      >
        &times;
      </button>
      <h3 style={{ marginTop: 0, marginBottom: 12 }}>How Ledgerly works</h3>
      <ol style={{ paddingLeft: 20, margin: 0, display: "grid", gap: 10, fontSize: 13, color: "var(--text-secondary)" }}>
        <li>
          <strong style={{ color: "var(--text-primary)" }}>Load your historic data.</strong> Upload a CSV of
          past transactions on the Insights page when you join (or load sample data first to see how it
          looks) -- the more history Ledgerly has, the better every prediction below.
        </li>
        <li>
          <strong style={{ color: "var(--text-primary)" }}>We score every customer automatically.</strong>{" "}
          Churn risk and predicted future value update the moment new data comes in -- no manual analysis
          on your end.
        </li>
        <li>
          <strong style={{ color: "var(--text-primary)" }}>Turn on the weekly digest.</strong> In Settings,
          get a Monday-morning summary of who's at risk and your biggest opportunity, without needing to log
          in and check.
        </li>
        <li>
          <strong style={{ color: "var(--text-primary)" }}>Predictions sharpen over time.</strong>{" "}
          Future-value forecasts get meaningfully more accurate once Ledgerly has seen a full quarter of
          your transaction history -- the calibration note below shows exactly which mode you're in right
          now.
        </li>
      </ol>
    </div>
  );
}

function calibrationStatusText(
  profile: BusinessProfileOut | null,
  businessTypes: BusinessTypeOption[] | null
): string | null {
  if (!profile) return null;
  if (profile.calibration_source === "calibrated") {
    return "Calibrated from your own transaction history.";
  }
  if (profile.calibration_source === "default_vertical") {
    const label = businessTypes?.find((opt) => opt.value === profile.business_type)?.label ?? "your business type";
    return `Using ${label} starting defaults — not enough of your own history yet.`;
  }
  return "Using generic starting defaults — set a business type in Settings for a better starting point.";
}

export default function Dashboard() {
  const [members, setMembers] = useState<MemberWithChurn[] | null>(null);
  const [transactions, setTransactions] = useState<TransactionOut[] | null>(null);
  const [alerts, setAlerts] = useState<FraudAlertOut[] | null>(null);
  const [futureValue, setFutureValue] = useState<FutureValueOut[] | null>(null);
  const [businessProfile, setBusinessProfile] = useState<BusinessProfileOut | null>(null);
  const [businessTypes, setBusinessTypes] = useState<BusinessTypeOption[] | null>(null);
  const [isSampleData, setIsSampleData] = useState(false);
  const [benchmark, setBenchmark] = useState<BenchmarkOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showJourney, setShowJourney] = useState(
    () => localStorage.getItem(JOURNEY_DISMISSED_KEY) !== "true"
  );

  function dismissJourney() {
    localStorage.setItem(JOURNEY_DISMISSED_KEY, "true");
    setShowJourney(false);
  }

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      listMembers(true),
      listTransactions(undefined, 10),
      getFraudAlerts(true),
      getFutureValue(90),
      getBusinessProfile(),
      listBusinessTypes(),
      getSampleDataStatus(),
      getBenchmark(),
    ])
      .then(([m, t, a, fv, profile, types, sampleStatus, benchmarkResult]) => {
        if (cancelled) return;
        setMembers(m);
        setTransactions(t);
        setAlerts(a);
        setFutureValue(fv);
        setBusinessProfile(profile);
        setBusinessTypes(types);
        setIsSampleData(sampleStatus.is_sample_data);
        setBenchmark(benchmarkResult);
      })
      .catch((err) => !cancelled && setError(String(err)));
    return () => {
      cancelled = true;
    };
  }, []);

  const loading = members === null || transactions === null || alerts === null || futureValue === null;

  // Leads with the insight, not an operational balance -- the total
  // predicted 90-day value across the book is "how much is at stake",
  // which is a more useful headline number than a raw points balance
  // that may not even match a member's real loyalty-app account.
  const totalFutureValue = futureValue?.reduce((sum, r) => sum + r.predicted_future_value, 0) ?? 0;
  const highRiskCount =
    members?.filter((m) => m.churn_risk_band === "high").length ?? 0;
  const unresolvedAlerts = alerts?.filter((a) => !a.resolved).length ?? 0;

  const memberNames = new Map(
    (members ?? []).map((m) => [m.id, `${m.first_name} ${m.last_name}`])
  );

  const statusText = calibrationStatusText(businessProfile, businessTypes);

  return (
    <div>
      <h2>Dashboard</h2>
      {error && <p className="error-text">{error}</p>}
      {showJourney && !loading && <OnboardingJourney onDismiss={dismissJourney} />}
      {isSampleData && !loading && (
        <div
          style={{
            background: "rgba(124,92,255,0.12)",
            border: "1px solid rgba(124,92,255,0.35)",
            borderRadius: 8,
            padding: "10px 14px",
            marginBottom: 12,
            fontSize: 13,
          }}
        >
          You're viewing sample data so you can see how Ledgerly looks for your business type.{" "}
          <Link to="/insights">Upload your own data</Link> whenever you're ready -- it'll replace the
          sample data automatically.
        </div>
      )}
      {statusText && !loading && (
        <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8, marginBottom: 16 }}>
          {statusText} <Link to="/settings">Change business type &rarr;</Link>
        </p>
      )}
      {benchmark?.available && !loading && (
        <div
          style={{
            background: "rgba(47,224,193,0.12)",
            border: "1px solid rgba(47,224,193,0.35)",
            borderRadius: 8,
            padding: "10px 14px",
            marginBottom: 16,
            fontSize: 13,
          }}
        >
          {benchmark.message}
        </div>
      )}
      {loading && !error ? (
        <p className="loading">Loading overview...</p>
      ) : (
        <>
          <div className="card-grid">
            <div className="card">
              <div className="label">Customers Tracked</div>
              <div className="value">{members!.length}</div>
            </div>
            <div className="card">
              <div className="label">Predicted 90-Day Value</div>
              <div className="value">{formatGBP(totalFutureValue)}</div>
            </div>
            <div className="card">
              <div className="label">High Churn Risk</div>
              <div className="value">{highRiskCount}</div>
            </div>
            <div className="card">
              <div className="label">Open Fraud Alerts</div>
              <div className="value">{unresolvedAlerts}</div>
            </div>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
            <section>
              <div className="toolbar">
                <h3 style={{ margin: 0 }}>Recent Activity</h3>
                <Link to="/members">View members &rarr;</Link>
              </div>
              {transactions!.length === 0 ? (
                <p className="empty">No activity yet.</p>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>Customer</th>
                      <th>Amount</th>
                      <th>Channel</th>
                      <th>Date</th>
                    </tr>
                  </thead>
                  <tbody>
                    {transactions!.map((t) => (
                      <tr key={t.id}>
                        <td>{memberNames.get(t.member_id) ?? t.member_id.slice(0, 8)}</td>
                        <td>{formatGBP(t.amount_gbp)}</td>
                        <td>{t.channel}</td>
                        <td>{formatDateUK(t.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            <section>
              <div className="toolbar">
                <h3 style={{ margin: 0 }}>Latest Fraud Alerts</h3>
                <Link to="/fraud-alerts">View all &rarr;</Link>
              </div>
              {alerts!.length === 0 ? (
                <p className="empty">No fraud alerts. Looking clean.</p>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>Reason</th>
                      <th>Score</th>
                    </tr>
                  </thead>
                  <tbody>
                    {alerts!.slice(0, 8).map((a) => (
                      <tr key={a.id}>
                        <td>{a.reason}</td>
                        <td>{a.score.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          </div>
        </>
      )}
    </div>
  );
}
