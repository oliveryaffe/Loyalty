import React, { useEffect, useState } from "react";

import { isApiError } from "../AuthContext";
import RiskBadge from "../components/RiskBadge";
import {
  getWinbackRule,
  getWinbackWorklist,
  listRewards,
  RewardOut,
  saveWinbackRule,
  WinbackRuleOut,
  WinbackWorklistEntry,
} from "../api/client";

/**
 * Win-back worklist: a read-only, computed-on-demand list of members at
 * risk of leaving and what to consider offering them. Reworked from an
 * auto-executing campaign feature -- Ledgerly never grants a reward or
 * writes to any loyalty ledger itself; it only surfaces the suggestion.
 * The merchant acts on it in whatever tool they already use to comp a
 * reward (Square, Loyalzoo, Stamp Me, or their own system). Route
 * /winback.
 */
export default function Winback() {
  const [rewards, setRewards] = useState<RewardOut[] | null>(null);
  const [rule, setRule] = useState<WinbackRuleOut | null>(null);
  const [worklist, setWorklist] = useState<WinbackWorklistEntry[] | null>(null);

  const [enabled, setEnabled] = useState(false);
  const [rewardId, setRewardId] = useState("");
  const [threshold, setThreshold] = useState(65);

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function load() {
    setLoading(true);
    Promise.all([listRewards(), getWinbackRule(), getWinbackWorklist()])
      .then(([rewardsList, ruleOut, worklistEntries]) => {
        setRewards(rewardsList);
        setRule(ruleOut);
        setWorklist(worklistEntries);
        setEnabled(ruleOut.enabled);
        setRewardId(ruleOut.reward_id ?? (rewardsList[0]?.id ?? ""));
        setThreshold(ruleOut.churn_risk_threshold);
      })
      .catch((err) => setError(isApiError(err) ? err.message : "Unable to load win-back settings."))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  async function handleSaveRule(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSaving(true);
    try {
      const saved = await saveWinbackRule({
        enabled,
        churn_risk_threshold: threshold,
        reward_id: rewardId,
      });
      setRule(saved);
      const worklistEntries = await getWinbackWorklist();
      setWorklist(worklistEntries);
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to save the win-back preference.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <h2>Win-back Worklist</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8, maxWidth: 640 }}>
        Members whose churn risk crosses your threshold, ranked highest-risk first. This is a suggestion list,
        not an automation -- Ledgerly doesn't grant rewards or touch any loyalty ledger. Comp the suggested
        reward yourself in whatever system you already use (Square, Loyalzoo, Stamp Me, or your own).
      </p>
      {error && <p className="error-text">{error}</p>}

      {loading ? (
        <p className="loading">Loading win-back settings...</p>
      ) : (
        <>
          <form
            onSubmit={handleSaveRule}
            className="card"
            style={{ marginBottom: 24, display: "grid", gap: 12, gridTemplateColumns: "1fr 1fr", maxWidth: 640 }}
          >
            <div className="field" style={{ gridColumn: "1 / -1" }}>
              <label>Reward to suggest</label>
              <select value={rewardId} onChange={(e) => setRewardId(e.target.value)} required>
                <option value="" disabled>
                  Select a reward...
                </option>
                {(rewards ?? []).map((r) => (
                  <option key={r.id} value={r.id} disabled={!r.active}>
                    {r.name}
                    {!r.active ? " (inactive)" : ""}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Churn risk threshold</label>
              <input
                type="number"
                min={0}
                max={100}
                value={threshold}
                onChange={(e) => setThreshold(Number(e.target.value))}
              />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 10, justifyContent: "center" }}>
              <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 8, color: "var(--text-secondary)" }}>
                <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                Show a suggested reward on the worklist
              </label>
            </div>
            <div style={{ gridColumn: "1 / -1", display: "flex", gap: 12, alignItems: "center" }}>
              <button className="primary" style={{ width: "auto", padding: "8px 20px" }} type="submit" disabled={saving || !rewardId}>
                {saving ? "Saving..." : "Save preference"}
              </button>
            </div>
            <p className="hint" style={{ gridColumn: "1 / -1", marginTop: 0 }}>
              The worklist below always shows who's at risk, even without a saved preference -- this form only
              controls which reward gets suggested alongside each name.
            </p>
          </form>

          <h3 style={{ marginTop: 32 }}>At risk right now</h3>
          {worklist === null || worklist.length === 0 ? (
            <p className="empty">No members currently above the threshold.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Member</th>
                  <th>Churn risk</th>
                  <th>Risk band</th>
                  <th>Suggested reward</th>
                </tr>
              </thead>
              <tbody>
                {worklist.map((entry) => (
                  <tr key={entry.member_id}>
                    <td>
                      {entry.first_name} {entry.last_name}
                    </td>
                    <td>{entry.churn_risk_score.toFixed(1)}</td>
                    <td>
                      <RiskBadge band={entry.risk_band} />
                    </td>
                    <td>{entry.suggested_reward_name ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}
