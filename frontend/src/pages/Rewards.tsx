import React, { useEffect, useState } from "react";

import { isApiError } from "../AuthContext";
import { createReward, listRewards, RewardOut } from "../api/client";

const EMPTY_FORM = {
  name: "",
  description: "",
  category: "general",
  points_cost: "",
  tier_required: "bronze",
};

export default function Rewards() {
  const [rewards, setRewards] = useState<RewardOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);

  function refresh() {
    listRewards()
      .then(setRewards)
      .catch((err) => setError(String(err)));
  }

  useEffect(refresh, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await createReward({
        name: form.name,
        description: form.description,
        category: form.category,
        points_cost: Number(form.points_cost),
        tier_required: form.tier_required,
      });
      setForm(EMPTY_FORM);
      setShowForm(false);
      refresh();
    } catch (err) {
      setError(isApiError(err) ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      <h2>Reward Categories</h2>
      <p style={{ color: "#94a3b8", fontSize: 13, maxWidth: 640, marginTop: -8, marginBottom: 8 }}>
        This isn't a separate rewards system to run -- it's how you tell Ledgerly what
        you're able to give a customer, so the AI can recommend something you can
        actually redeem instead of a generic category.
      </p>
      <p style={{ color: "#94a3b8", fontSize: 13, maxWidth: 640, marginTop: 0, marginBottom: 20 }}>
        <strong style={{ color: "#f5f6fa" }}>If you already run a loyalty or rewards programme</strong>{" "}
        (Square, Loyalzoo, Stamp Me, or your own), add each tier and its point cost below
        -- mirror what a customer can redeem in real life. <strong style={{ color: "#f5f6fa" }}>
        If you don't run one</strong>, you can leave this empty; nothing else on the
        dashboard requires it. Next Best Product recommendations will just describe a
        purchase category (e.g. "coffee") instead of a specific redeemable reward.
      </p>
      {error && <p className="error-text">{error}</p>}

      <div className="toolbar">
        <span style={{ fontSize: 13, color: "#94a3b8" }}>
          {rewards ? `${rewards.length} categories` : ""}
        </span>
        <button className="primary" style={{ width: "auto", padding: "8px 16px" }} onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Cancel" : "+ New Category"}
        </button>
      </div>

      {showForm && (
        <form
          onSubmit={handleSubmit}
          className="card"
          style={{ marginBottom: 24, display: "grid", gap: 12, gridTemplateColumns: "1fr 1fr" }}
        >
          <div className="field">
            <label>Name</label>
            <input
              required
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            />
          </div>
          <div className="field">
            <label>Category</label>
            <input
              value={form.category}
              onChange={(e) => setForm((f) => ({ ...f, category: e.target.value }))}
            />
          </div>
          <div className="field">
            <label>Points Cost</label>
            <input
              required
              type="number"
              min={0}
              value={form.points_cost}
              onChange={(e) => setForm((f) => ({ ...f, points_cost: e.target.value }))}
            />
          </div>
          <div className="field">
            <label>Tier Required</label>
            <select
              value={form.tier_required}
              onChange={(e) => setForm((f) => ({ ...f, tier_required: e.target.value }))}
            >
              <option value="bronze">bronze</option>
              <option value="silver">silver</option>
              <option value="gold">gold</option>
              <option value="platinum">platinum</option>
            </select>
          </div>
          <div className="field" style={{ gridColumn: "1 / -1" }}>
            <label>Description</label>
            <input
              value={form.description}
              onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            />
          </div>
          <div style={{ gridColumn: "1 / -1" }}>
            <button className="primary" type="submit" disabled={submitting}>
              {submitting ? "Saving..." : "Save Category"}
            </button>
          </div>
        </form>
      )}

      {rewards === null ? (
        <p className="loading">Loading rewards...</p>
      ) : rewards.length === 0 ? (
        <p className="empty">
          No reward categories yet. If you run a loyalty or rewards programme, click
          "+ New Category" above and add each tier so recommendations map to something
          redeemable. If you don't run one, there's nothing to set up here -- this can
          stay empty.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Category</th>
              <th>Points Cost</th>
              <th>Tier Required</th>
              <th>Active</th>
            </tr>
          </thead>
          <tbody>
            {rewards.map((r) => (
              <tr key={r.id}>
                <td>
                  {r.name}
                  {r.description && (
                    <>
                      <br />
                      <span style={{ fontSize: 12, color: "#94a3b8" }}>{r.description}</span>
                    </>
                  )}
                </td>
                <td>{r.category}</td>
                <td>{r.points_cost.toLocaleString()}</td>
                <td>{r.tier_required}</td>
                <td>{r.active ? "Yes" : "No"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
