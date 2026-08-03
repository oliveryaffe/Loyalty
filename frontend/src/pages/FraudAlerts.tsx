import React, { useEffect, useState } from "react";

import { FraudAlertOut, getFraudAlerts } from "../api/client";

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

  return (
    <div>
      <h2>Fraud &amp; Anomaly Alerts</h2>
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
              <th>Member</th>
              <th>Reason</th>
              <th>Details</th>
              <th>Score</th>
              <th>Status</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((a) => (
              <tr key={a.id}>
                <td>{a.member_id.slice(0, 8)}</td>
                <td>{a.reason}</td>
                <td style={{ fontSize: 12, color: "#94a3b8" }}>{a.details}</td>
                <td>
                  <span className={`badge badge-${riskLevel(a.score)}`}>
                    {a.score.toFixed(2)}
                  </span>
                </td>
                <td>{a.resolved ? "Resolved" : "Open"}</td>
                <td>{new Date(a.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
