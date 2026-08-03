import React, { useEffect, useState } from "react";

import { isApiError } from "../AuthContext";
import {
  listRewards,
  listWinbackOffers,
  getWinbackRule,
  RewardOut,
  runWinback,
  saveWinbackRule,
  WinbackOfferOut,
  WinbackRuleOut,
  WinbackRunResult,
} from "../api/client";
import { formatDateTimeUK } from "../utils";

/**
 * Win-back campaigns (PLAN_BATCH3.md §4): one rule per merchant, a manual
 * "send offers now" trigger, and an optional auto-trigger piggybacking on
 * §3's escalation detection. Route /winback, per the plan's explicit
 * frontend spec.
 */
export default function Winback() {
  const [rewards, setRewards] = useState<RewardOut[] | null>(null);
  const [rule, setRule] = useState<WinbackRuleOut | null>(null);
  const [offers, setOffers] = useState<WinbackOfferOut[] | null>(null);

  const [enabled, setEnabled] = useState(false);
  const [rewardId, setRewardId] = useState("");
  const [threshold, setThreshold] = useState(65);
  const [autoTrigger, setAutoTrigger] = useState(false);

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runResult, setRunResult] = useState<WinbackRunResult | null>(null);

  function load() {
    setLoading(true);
    Promise.all([listRewards(), getWinbackRule(), listWinbackOffers()])
      .then(([rewardsList, ruleOut, offersList]) => {
        setRewards(rewardsList);
        setRule(ruleOut);
        setOffers(offersList);
        setEnabled(ruleOut.enabled);
        setRewardId(ruleOut.reward_id ?? (rewardsList[0]?.id ?? ""));
        setThreshold(ruleOut.churn_risk_threshold);
        setAutoTrigger(ruleOut.auto_trigger);
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
        auto_trigger: autoTrigger,
      });
      setRule(saved);
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to save the win-back rule.");
    } finally {
      setSaving(false);
    }
  }

  async function handleRunNow() {
    setError(null);
    setRunResult(null);
    setRunning(true);
    try {
      const result = await runWinback();
      setRunResult(result);
      const offersList = await listWinbackOffers();
      setOffers(offersList);
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to run win-back offers.");
    } finally {
      setRunning(false);
    }
  }

  const rewardNameById = Object.fromEntries((rewards ?? []).map((r) => [r.id, r.name]));

  // WinbackOfferOut only carries `rule_id` (not `reward_id` directly, since
  // the underlying WinbackOffer/WinbackRule tables don't duplicate it) --
  // resolve a display name for the common case (the offer was sent under
  // the currently-saved rule); older offers from a rule that's since been
  // edited just show a generic label rather than a stale/misleading name.
  function rewardNameForOffer(offer: WinbackOfferOut): string {
    if (rule?.id && offer.rule_id === rule.id && rule.reward_id) {
      return rewardNameById[rule.reward_id] ?? "—";
    }
    return "(rule since updated)";
  }

  return (
    <div>
      <h2>Win-back Campaigns</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8 }}>
        Automatically comp a reward to members whose churn risk crosses your threshold. Rewards are granted for
        free (no points debited) and each member is offered at most once, ever.
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
              <label>Reward to offer</label>
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
                Enabled
              </label>
              <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 8, color: "var(--text-secondary)" }}>
                <input type="checkbox" checked={autoTrigger} onChange={(e) => setAutoTrigger(e.target.checked)} />
                Auto-send when a member escalates to high risk
              </label>
            </div>
            <div style={{ gridColumn: "1 / -1", display: "flex", gap: 12, alignItems: "center" }}>
              <button className="primary" style={{ width: "auto", padding: "8px 20px" }} type="submit" disabled={saving || !rewardId}>
                {saving ? "Saving..." : "Save rule"}
              </button>
              <button
                type="button"
                className="secondary"
                onClick={handleRunNow}
                disabled={running || !rule?.enabled}
                title={!rule?.enabled ? "Save an enabled rule first" : undefined}
              >
                {running ? "Sending..." : "Send win-back offers now"}
              </button>
            </div>
            <p className="hint" style={{ gridColumn: "1 / -1", marginTop: 0 }}>
              Auto-trigger is off by default -- with it off, offers are only ever sent when you click "Send
              win-back offers now" above, even if members cross the threshold.
            </p>
          </form>

          {runResult && (
            <div className="upload-banner" style={{ maxWidth: 640 }}>
              <strong>Run complete.</strong> {runResult.offers_sent} offer(s) sent
              {runResult.member_ids.length > 0 ? ` to ${runResult.member_ids.length} member(s).` : "."}
            </div>
          )}

          <h3 style={{ marginTop: 32 }}>Offer history</h3>
          {offers === null || offers.length === 0 ? (
            <p className="empty">No win-back offers sent yet.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Member</th>
                  <th>Reward</th>
                  <th>Churn score at trigger</th>
                  <th>Triggered by</th>
                  <th>Sent</th>
                </tr>
              </thead>
              <tbody>
                {offers.map((o) => (
                  <tr key={o.id}>
                    <td>{o.member_id.slice(0, 8)}</td>
                    <td>{rewardNameForOffer(o)}</td>
                    <td>{o.churn_risk_score_at_trigger.toFixed(1)}</td>
                    <td>
                      <span className={`badge ${o.triggered_by === "auto" ? "badge-medium" : "badge-low"}`}>
                        {o.triggered_by}
                      </span>
                    </td>
                    <td>{formatDateTimeUK(o.created_at)}</td>
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
