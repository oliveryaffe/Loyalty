import React, { useEffect, useState } from "react";

import { FraudAlertOut, getFraudAlerts } from "../api/client";
import { formatDateTimeUK } from "../utils";

export default function FraudAlerts() {
  const [alerts, setAlerts] = useState<FraudAlertOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [showResolved, setShowResolved] = useState(true);

  function load(refresh: boolean) {
    setRefreshing(refresh);
    getFraudAlerts(refresh)
      .then(setAlerts)
      .catch((err) => setError(String(err)))
      .finally(() => setRefreshing(false));
  }

  useEffect(() => load(true), []);

  const visible = alerts?.filter((a) => showResolved || !a.resolved) ?? [];

  function riskLevel(score: number): "low" | "medium" | "high" {
    if (score >= 5) return "high";
    if (score >= 3) return "medium";
    return "low";
  }

  function severityLabel(score: number): string {
    const level = riskLevel(score);
    return level === "high" ? "High severity" : level === "medium" ? "Medium severity" : "Low severity";
  }

  function reasonLabel(reason: string): string {
    const parts = reason.split("+");
    const labels = parts.map((p) =>
      p === "abnormal_amount"
        ? "Unusual amount"
        : p === "abnormal_velocity"
        ? "Rapid activity"
        : p.replace(/_/g, " ")
    );
    return labels.join(" + ");
  }

  return (
    <div>
      <h2>Fraud &amp; Anomaly Alerts</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8, maxWidth: 640 }}>
        Two automatic checks flag a transaction: an amount that's a big outlier for that
        customer's own history, or points earned unusually fast across several transactions
        in a short window (bot-like or points-farming behaviour). Nothing is blocked or
        refunded automatically -- this is a worklist for you to review and resolve.
      </p>
      {error && <p className="error-text">{error}</p>}

      <div className="toolbar">
        <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
          <input
            type="checkbox"
            checked={showResolved}
            onChange={(e) => setShowResolved(e.target.checked)}
          />
          Show resolved
        </label>
        <button
          className="pill-select"
          onClick={() => load(true)}
          disabled={refreshing}
        >
          {refreshing ? "Re-scanning..." : "Re-run detection"}
        </button>
      </div>

      {alerts === null ? (
        <p className="loading">Loading fraud alerts...</p>
      ) : visible.length === 0 ? (
        <p className="empty">No fraud alerts to show.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Customer</th>
              <th>What we found</th>
              <th>Severity</th>
              <th>Status</th>
              <th>Flagged</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((a) => (
              <tr key={a.id}>
                <td>{a.member_name}</td>
                <td style={{ maxWidth: 420 }}>
                  <div>{a.explanation}</div>
                  <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>{reasonLabel(a.reason)}</div>
                </td>
                <td>
                  <span className={`badge badge-${riskLevel(a.score)}`} title={`Raw score: ${a.score.toFixed(2)}`}>
                    {severityLabel(a.score)}
                  </span>
                </td>
                <td>{a.resolved ? "Resolved" : "Open"}</td>
                <td>{formatDateTimeUK(a.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
