import React, { useEffect, useState } from "react";

import { isApiError } from "../AuthContext";
import {
  createExperiment,
  endExperiment,
  ExperimentDetailOut,
  ExperimentOut,
  ExperimentResultsOut,
  getExperiment,
  getExperimentResults,
  listExperiments,
  listRewards,
  RewardOut,
} from "../api/client";
import { formatDateTimeUK } from "../utils";

/**
 * A/B testing for reward structures (PLAN_BATCH3.md §5) -- deliberately the
 * smallest-scope shape the plan calls for: a create form that bulk-assigns
 * every active member to variant A or B at creation time, a list of past/
 * running experiments, and a results-comparison view. Route /experiments,
 * per the plan's explicit frontend spec. No member-facing UI exists in this
 * product -- "assignment" is a backend cohort split that steers reward
 * recommendations and is measured via redemption behavior, not a literal
 * two-versions-of-a-webpage split.
 */
export default function Experiments() {
  const [rewards, setRewards] = useState<RewardOut[] | null>(null);
  const [experiments, setExperiments] = useState<ExperimentOut[] | null>(null);

  const [name, setName] = useState("");
  const [variantARewardId, setVariantARewardId] = useState("");
  const [variantBRewardId, setVariantBRewardId] = useState("");
  const [trafficSplit, setTrafficSplit] = useState(0.5);

  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ExperimentDetailOut | null>(null);
  const [results, setResults] = useState<ExperimentResultsOut | null>(null);
  const [resultsLoading, setResultsLoading] = useState(false);
  const [ending, setEnding] = useState(false);

  function load() {
    setLoading(true);
    Promise.all([listRewards(), listExperiments()])
      .then(([rewardsList, experimentsList]) => {
        setRewards(rewardsList);
        setExperiments(experimentsList);
        const active = rewardsList.filter((r) => r.active);
        setVariantARewardId(active[0]?.id ?? "");
        setVariantBRewardId(active[1]?.id ?? active[0]?.id ?? "");
      })
      .catch((err) => setError(isApiError(err) ? err.message : "Unable to load experiments."))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setCreating(true);
    try {
      const created = await createExperiment({
        name,
        variant_a_reward_id: variantARewardId,
        variant_b_reward_id: variantBRewardId,
        traffic_split: trafficSplit,
      });
      setName("");
      const experimentsList = await listExperiments();
      setExperiments(experimentsList);
      await viewResults(created.id);
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to create the experiment.");
    } finally {
      setCreating(false);
    }
  }

  async function viewResults(id: string) {
    setError(null);
    setSelectedId(id);
    setResultsLoading(true);
    setResults(null);
    setDetail(null);
    try {
      const [detailOut, resultsOut] = await Promise.all([getExperiment(id), getExperimentResults(id)]);
      setDetail(detailOut);
      setResults(resultsOut);
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to load experiment results.");
    } finally {
      setResultsLoading(false);
    }
  }

  async function handleEnd() {
    if (!selectedId) return;
    setEnding(true);
    setError(null);
    try {
      await endExperiment(selectedId);
      const experimentsList = await listExperiments();
      setExperiments(experimentsList);
      await viewResults(selectedId);
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to end the experiment.");
    } finally {
      setEnding(false);
    }
  }

  const rewardNameById = Object.fromEntries((rewards ?? []).map((r) => [r.id, r.name]));
  const activeRewards = (rewards ?? []).filter((r) => r.active);

  return (
    <div>
      <h2>A/B Testing</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8 }}>
        Compare two reward variants against a random split of your members. Ledgerly has no separate
        member-facing storefront, so "assignment" is a backend cohort split that steers each member's
        recommendations toward their assigned variant -- results are measured by comparing redemption
        behavior between the two groups.
      </p>
      {error && <p className="error-text">{error}</p>}

      {loading ? (
        <p className="loading">Loading experiments...</p>
      ) : (
        <>
          <form
            onSubmit={handleCreate}
            className="card"
            style={{ marginBottom: 24, display: "grid", gap: 12, gridTemplateColumns: "1fr 1fr", maxWidth: 720 }}
          >
            <div className="field" style={{ gridColumn: "1 / -1" }}>
              <label>Experiment name</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. £5 voucher vs. double points"
                required
              />
            </div>
            <div className="field">
              <label>Variant A reward</label>
              <select value={variantARewardId} onChange={(e) => setVariantARewardId(e.target.value)} required>
                <option value="" disabled>
                  Select a reward...
                </option>
                {activeRewards.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Variant B reward</label>
              <select value={variantBRewardId} onChange={(e) => setVariantBRewardId(e.target.value)} required>
                <option value="" disabled>
                  Select a reward...
                </option>
                {activeRewards.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ gridColumn: "1 / -1" }}>
              <label>Traffic split -- share of members assigned to variant B: {Math.round(trafficSplit * 100)}%</label>
              <input
                type="range"
                min={0.05}
                max={0.95}
                step={0.05}
                value={trafficSplit}
                onChange={(e) => setTrafficSplit(Number(e.target.value))}
              />
            </div>
            <div style={{ gridColumn: "1 / -1" }}>
              <button
                className="primary"
                style={{ width: "auto", padding: "8px 20px" }}
                type="submit"
                disabled={creating || !variantARewardId || !variantBRewardId || variantARewardId === variantBRewardId}
              >
                {creating ? "Creating..." : "Create experiment"}
              </button>
              {variantARewardId && variantARewardId === variantBRewardId && (
                <span className="hint" style={{ marginLeft: 12 }}>
                  Variant A and B must be different rewards.
                </span>
              )}
            </div>
            <p className="hint" style={{ gridColumn: "1 / -1", marginTop: 0 }}>
              Every active member is randomly assigned to variant A or B once, immediately, when the
              experiment is created. Members added later are not automatically assigned.
            </p>
          </form>

          <h3>Experiments</h3>
          {experiments === null || experiments.length === 0 ? (
            <p className="empty">No experiments yet -- create one above.</p>
          ) : (
            <table style={{ marginBottom: 24 }}>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Variant A</th>
                  <th>Variant B</th>
                  <th>Status</th>
                  <th>Started</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {experiments.map((exp) => (
                  <tr key={exp.id}>
                    <td>{exp.name}</td>
                    <td>{rewardNameById[exp.variant_a_reward_id] ?? "—"}</td>
                    <td>{rewardNameById[exp.variant_b_reward_id] ?? "—"}</td>
                    <td>
                      <span className={`badge ${exp.status === "running" ? "badge-low" : "badge-medium"}`}>
                        {exp.status}
                      </span>
                    </td>
                    <td>{formatDateTimeUK(exp.started_at)}</td>
                    <td>
                      <button
                        type="button"
                        className="secondary"
                        style={{ width: "auto", padding: "4px 14px" }}
                        onClick={() => viewResults(exp.id)}
                      >
                        View results
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {selectedId && (
            <div className="card" style={{ maxWidth: 720 }}>
              {resultsLoading || !results || !detail ? (
                <p className="loading">Loading results...</p>
              ) : (
                <ResultsPanel
                  detail={detail}
                  results={results}
                  onEnd={handleEnd}
                  ending={ending}
                />
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function VariantBar({ label, rate, color }: { label: string; rate: number; color: string }) {
  const widthPct = Math.max(2, Math.min(100, rate * 100));
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 4 }}>
        <span>{label}</span>
        <span style={{ color: "var(--text-secondary)" }}>{(rate * 100).toFixed(1)}%</span>
      </div>
      <div style={{ background: "rgba(255,255,255,0.06)", borderRadius: 6, height: 10, overflow: "hidden" }}>
        <div style={{ width: `${widthPct}%`, background: color, height: "100%", borderRadius: 6 }} />
      </div>
    </div>
  );
}

function ResultsPanel({
  detail,
  results,
  onEnd,
  ending,
}: {
  detail: ExperimentDetailOut;
  results: ExperimentResultsOut;
  onEnd: () => void;
  ending: boolean;
}) {
  const winnerLabel =
    results.directional_winner === "inconclusive"
      ? "Inconclusive -- no clear winner yet"
      : `Variant ${results.directional_winner.toUpperCase()} is directionally ahead`;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <h3 style={{ margin: 0 }}>{detail.name}</h3>
          <p style={{ color: "var(--text-secondary)", fontSize: 13, margin: "4px 0 0" }}>
            {detail.members_assigned_a} members in A · {detail.members_assigned_b} members in B
          </p>
        </div>
        <span className={`badge ${detail.status === "running" ? "badge-low" : "badge-medium"}`}>
          {detail.status}
        </span>
      </div>

      <div style={{ marginTop: 20 }}>
        <VariantBar label={`A -- ${results.variant_a.reward_name}`} rate={results.variant_a.redemption_rate} color="var(--plum)" />
        <VariantBar label={`B -- ${results.variant_b.reward_name}`} rate={results.variant_b.redemption_rate} color="var(--mint)" />
      </div>

      <table style={{ marginTop: 8 }}>
        <thead>
          <tr>
            <th></th>
            <th>Assigned</th>
            <th>Redeemed</th>
            <th>Redemption rate</th>
            <th>Points spent</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>A -- {results.variant_a.reward_name}</td>
            <td>{results.variant_a.members_assigned}</td>
            <td>{results.variant_a.redemptions_count}</td>
            <td>{(results.variant_a.redemption_rate * 100).toFixed(1)}%</td>
            <td>{results.variant_a.total_points_spent.toLocaleString()}</td>
          </tr>
          <tr>
            <td>B -- {results.variant_b.reward_name}</td>
            <td>{results.variant_b.members_assigned}</td>
            <td>{results.variant_b.redemptions_count}</td>
            <td>{(results.variant_b.redemption_rate * 100).toFixed(1)}%</td>
            <td>{results.variant_b.total_points_spent.toLocaleString()}</td>
          </tr>
        </tbody>
      </table>

      <div className="upload-banner" style={{ marginTop: 16 }}>
        <strong>{winnerLabel}</strong>
        {results.z_score !== null && <span> (z = {results.z_score.toFixed(2)})</span>}
        <p style={{ margin: "6px 0 0", fontSize: 12, color: "var(--text-secondary)" }}>{results.sample_size_caveat}</p>
      </div>

      {detail.status === "running" && (
        <button
          type="button"
          className="secondary"
          style={{ marginTop: 16, width: "auto", padding: "6px 18px" }}
          onClick={onEnd}
          disabled={ending}
        >
          {ending ? "Ending..." : "End experiment"}
        </button>
      )}
    </div>
  );
}
