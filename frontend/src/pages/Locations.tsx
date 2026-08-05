import React, { useEffect, useState } from "react";

import { isApiError } from "../AuthContext";
import { createLocation, getLocationRollup, LocationRollupOut } from "../api/client";
import { formatGBP } from "../utils";

/**
 * Multi-location roll-up (competitive-brief backlog item #6): a
 * lightweight "view across your N shops" summary -- member count, high
 * churn-risk count, and predicted 90-day value per location, plus a
 * trailing "Unassigned" row for any member with no location set.
 * Entirely opt-in: a single-location merchant never needs to visit this
 * page. Members are assigned to a location from the member detail modal
 * on the Members page.
 */
export default function Locations() {
  const [rollup, setRollup] = useState<LocationRollupOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newLocationName, setNewLocationName] = useState("");
  const [creating, setCreating] = useState(false);

  function load() {
    getLocationRollup()
      .then(setRollup)
      .catch((err) => setError(isApiError(err) ? err.message : "Unable to load locations."));
  }

  useEffect(load, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newLocationName.trim()) return;
    setError(null);
    setCreating(true);
    try {
      await createLocation(newLocationName.trim());
      setNewLocationName("");
      load();
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to create location.");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div>
      <h2>Locations</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8, maxWidth: 640 }}>
        A roll-up across every shop on this account. Add a location below, then assign members to it from
        their profile on the Members page -- members with no location set show up under "Unassigned".
      </p>
      {error && <p className="error-text">{error}</p>}

      <form onSubmit={handleCreate} className="toolbar" style={{ marginBottom: 20 }}>
        <input
          className="pill-select"
          style={{ minWidth: 240 }}
          placeholder="New location name (e.g. High Street)"
          value={newLocationName}
          onChange={(e) => setNewLocationName(e.target.value)}
        />
        <button className="primary" style={{ width: "auto", padding: "8px 20px" }} type="submit" disabled={creating}>
          {creating ? "Adding..." : "Add location"}
        </button>
      </form>

      {rollup === null ? (
        <p className="loading">Loading locations...</p>
      ) : rollup.length === 0 ? (
        <p className="empty">No locations yet -- add one above to start splitting your data by shop.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Location</th>
              <th>Members</th>
              <th>High Churn Risk</th>
              <th>Predicted 90-Day Value</th>
            </tr>
          </thead>
          <tbody>
            {rollup.map((row) => (
              <tr key={row.location_id ?? "unassigned"}>
                <td>{row.name}</td>
                <td>{row.member_count}</td>
                <td>{row.high_risk_count}</td>
                <td>{formatGBP(row.predicted_value_90d)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
