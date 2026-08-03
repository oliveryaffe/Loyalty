import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  FraudAlertOut,
  getFraudAlerts,
  listMembers,
  listTransactions,
  MemberWithChurn,
  TransactionOut,
} from "../api/client";
import { formatGBP } from "../utils";

export default function Dashboard() {
  const [members, setMembers] = useState<MemberWithChurn[] | null>(null);
  const [transactions, setTransactions] = useState<TransactionOut[] | null>(null);
  const [alerts, setAlerts] = useState<FraudAlertOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      listMembers(true),
      listTransactions(undefined, 10),
      getFraudAlerts(true),
    ])
      .then(([m, t, a]) => {
        if (cancelled) return;
        setMembers(m);
        setTransactions(t);
        setAlerts(a);
      })
      .catch((err) => !cancelled && setError(String(err)));
    return () => {
      cancelled = true;
    };
  }, []);

  const loading = members === null || transactions === null || alerts === null;

  const totalPoints = members?.reduce((sum, m) => sum + m.points_balance, 0) ?? 0;
  const highRiskCount =
    members?.filter((m) => m.churn_risk_band === "high").length ?? 0;
  const unresolvedAlerts = alerts?.filter((a) => !a.resolved).length ?? 0;

  return (
    <div>
      <h2>Dashboard</h2>
      {error && <p className="error-text">{error}</p>}
      {loading && !error ? (
        <p className="loading">Loading overview...</p>
      ) : (
        <>
          <div className="card-grid">
            <div className="card">
              <div className="label">Members</div>
              <div className="value">{members!.length}</div>
            </div>
            <div className="card">
              <div className="label">Points Outstanding</div>
              <div className="value">{totalPoints.toLocaleString()}</div>
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
                <h3 style={{ margin: 0 }}>Recent Transactions</h3>
                <Link to="/members">View members &rarr;</Link>
              </div>
              {transactions!.length === 0 ? (
                <p className="empty">No transactions yet.</p>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>Member</th>
                      <th>Type</th>
                      <th>Amount</th>
                      <th>Points</th>
                      <th>Channel</th>
                    </tr>
                  </thead>
                  <tbody>
                    {transactions!.map((t) => (
                      <tr key={t.id}>
                        <td>{t.member_id.slice(0, 8)}</td>
                        <td>{t.type}</td>
                        <td>{formatGBP(t.amount_gbp)}</td>
                        <td>{t.points > 0 ? `+${t.points}` : t.points}</td>
                        <td>{t.channel}</td>
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
