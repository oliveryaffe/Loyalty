import React, { useEffect, useMemo, useState } from "react";

import { isApiError } from "../AuthContext";
import {
  assignMemberLocation,
  AudienceExportFormat,
  downloadAudienceExport,
  getRecommendations,
  listLocations,
  listMembers,
  LocationOut,
  MemberWithChurn,
  RecommendationOut,
} from "../api/client";
import RiskBadge from "../components/RiskBadge";
import { formatDateUK } from "../utils";

type SortKey =
  | "name"
  | "tier"
  | "points_balance"
  | "churn_risk_score"
  | "last_activity_at"
  | "next_visit_days_overdue";

type RiskFilter = "all" | "high" | "medium" | "low";

export default function Members() {
  const [members, setMembers] = useState<MemberWithChurn[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [riskFilter, setRiskFilter] = useState<RiskFilter>("all");
  const [sortKey, setSortKey] = useState<SortKey>("churn_risk_score");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [selectedMember, setSelectedMember] = useState<MemberWithChurn | null>(
    null
  );
  const [recommendations, setRecommendations] = useState<RecommendationOut[] | null>(
    null
  );
  const [recLoading, setRecLoading] = useState(false);
  const [exportFormat, setExportFormat] = useState<AudienceExportFormat>("generic");
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [locations, setLocations] = useState<LocationOut[] | null>(null);
  const [savingLocation, setSavingLocation] = useState(false);

  useEffect(() => {
    listMembers(true)
      .then(setMembers)
      .catch((err) => setError(String(err)));
    listLocations()
      .then(setLocations)
      .catch(() => setLocations([])); // non-critical -- just disables the picker
  }, []);

  async function handleLocationChange(locationId: string) {
    if (!selectedMember) return;
    setSavingLocation(true);
    try {
      await assignMemberLocation(selectedMember.id, locationId || null);
      const updatedMember = { ...selectedMember, location_id: locationId || null };
      setSelectedMember(updatedMember);
      setMembers((prev) => prev?.map((m) => (m.id === updatedMember.id ? updatedMember : m)) ?? prev);
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to update location.");
    } finally {
      setSavingLocation(false);
    }
  }

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  const filtered = useMemo(() => {
    if (!members) return [];
    const q = search.trim().toLowerCase();
    let rows = members;
    if (q) {
      rows = rows.filter(
        (m) =>
          `${m.first_name} ${m.last_name}`.toLowerCase().includes(q) ||
          m.email.toLowerCase().includes(q)
      );
    }
    if (riskFilter !== "all") {
      rows = rows.filter((m) => m.churn_risk_band === riskFilter);
    }
    const sorted = [...rows].sort((a, b) => {
      let av: number | string;
      let bv: number | string;
      switch (sortKey) {
        case "name":
          av = `${a.first_name} ${a.last_name}`;
          bv = `${b.first_name} ${b.last_name}`;
          break;
        case "tier":
          av = a.tier;
          bv = b.tier;
          break;
        case "points_balance":
          av = a.points_balance;
          bv = b.points_balance;
          break;
        case "last_activity_at":
          av = a.last_activity_at;
          bv = b.last_activity_at;
          break;
        case "next_visit_days_overdue":
          // Most-overdue first when sorting this column -- members with
          // no prediction yet sort last, not first (a null shouldn't
          // outrank someone who's genuinely 40 days overdue).
          av = a.next_visit_days_overdue ?? -1;
          bv = b.next_visit_days_overdue ?? -1;
          break;
        case "churn_risk_score":
        default:
          av = a.churn_risk_score ?? -1;
          bv = b.churn_risk_score ?? -1;
          break;
      }
      if (av < bv) return sortDir === "asc" ? -1 : 1;
      if (av > bv) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
    return sorted;
  }, [members, search, riskFilter, sortKey, sortDir]);

  async function handleExport() {
    setExportError(null);
    setExporting(true);
    try {
      await downloadAudienceExport(riskFilter === "all" ? null : riskFilter, exportFormat);
    } catch (err) {
      setExportError(isApiError(err) ? err.message : "Unable to export audience.");
    } finally {
      setExporting(false);
    }
  }

  function openMember(m: MemberWithChurn) {
    setSelectedMember(m);
    setRecommendations(null);
    setRecLoading(true);
    getRecommendations(m.id, 5)
      .then(setRecommendations)
      .catch((err) => setError(String(err)))
      .finally(() => setRecLoading(false));
  }

  return (
    <div>
      <h2>Customers</h2>
      {error && <p className="error-text">{error}</p>}

      <div className="toolbar">
        <input
          className="pill-select"
          style={{ minWidth: 260 }}
          placeholder="Search by name or email..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className="pill-select"
          value={riskFilter}
          onChange={(e) => setRiskFilter(e.target.value as RiskFilter)}
          aria-label="Filter by churn risk"
        >
          <option value="all">All risk levels</option>
          <option value="high">High risk only</option>
          <option value="medium">Medium risk only</option>
          <option value="low">Low risk only</option>
        </select>
        <span style={{ fontSize: 13, color: "#94a3b8" }}>
          {members ? `${filtered.length} of ${members.length} customers` : ""}
        </span>
        <select
          className="pill-select"
          value={exportFormat}
          onChange={(e) => setExportFormat(e.target.value as AudienceExportFormat)}
          aria-label="Export format"
        >
          <option value="generic">Generic CSV</option>
          <option value="mailchimp">Mailchimp</option>
          <option value="klaviyo">Klaviyo</option>
        </select>
        <button type="button" className="secondary" onClick={handleExport} disabled={exporting}>
          {exporting
            ? "Exporting..."
            : riskFilter === "all"
            ? "Export audience"
            : `Export ${riskFilter}-risk audience`}
        </button>
      </div>
      {exportError && <p className="error-text">{exportError}</p>}
      <p className="hint" style={{ marginTop: -8 }}>
        Exports the list currently filtered above (by risk level) as a CSV ready to import into Mailchimp
        or Klaviyo -- act on the insight wherever you already message customers.
      </p>

      {members === null ? (
        <p className="loading">Loading customers...</p>
      ) : filtered.length === 0 ? (
        <p className="empty">No customers match your search.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th onClick={() => toggleSort("name")}>Name</th>
              <th>Email</th>
              <th onClick={() => toggleSort("tier")}>Tier</th>
              <th onClick={() => toggleSort("points_balance")}>Points</th>
              <th onClick={() => toggleSort("last_activity_at")}>
                Last Activity
              </th>
              <th onClick={() => toggleSort("next_visit_days_overdue")}>
                Next Visit
              </th>
              <th onClick={() => toggleSort("churn_risk_score")}>
                Churn Risk
              </th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((m) => (
              <tr
                key={m.id}
                onClick={() => openMember(m)}
                style={{ cursor: "pointer" }}
              >
                <td>
                  {m.first_name} {m.last_name}
                </td>
                <td>{m.email}</td>
                <td>{m.tier}</td>
                <td>{m.points_balance.toLocaleString()}</td>
                <td>{formatDateUK(m.last_activity_at)}</td>
                <td>
                  {m.predicted_next_visit_date ? (
                    <>
                      {formatDateUK(m.predicted_next_visit_date)}
                      {m.next_visit_days_overdue !== null && (
                        <div style={{ fontSize: 11, color: "var(--coral)", marginTop: 2 }}>
                          {m.next_visit_days_overdue}d overdue
                        </div>
                      )}
                    </>
                  ) : (
                    <span style={{ color: "var(--text-muted)" }}>Not enough history</span>
                  )}
                </td>
                <td style={{ minWidth: 220, maxWidth: 320 }}>
                  <div>
                    <RiskBadge band={m.churn_risk_band} />{" "}
                    {m.churn_risk_score !== null ? `${Math.round(m.churn_risk_score)}` : "-"}
                  </div>
                  {m.churn_risk_explanation && (
                    <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 3, lineHeight: 1.4 }}>
                      {m.churn_risk_explanation}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selectedMember && (
        <div
          role="dialog"
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(11,14,20,0.75)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 10,
          }}
          onClick={() => setSelectedMember(null)}
        >
          <div
            className="login-card modal-card"
            style={{ width: 420 }}
            onClick={(e) => e.stopPropagation()}
          >
            <h1>
              {selectedMember.first_name} {selectedMember.last_name}
            </h1>
            <p className="subtitle">
              {selectedMember.email} &middot; {selectedMember.tier} &middot;{" "}
              {selectedMember.points_balance.toLocaleString()} pts
            </p>
            <p style={{ fontSize: 13 }}>
              Churn risk: <RiskBadge band={selectedMember.churn_risk_band} />{" "}
              {selectedMember.churn_risk_score !== null
                ? `${Math.round(selectedMember.churn_risk_score)} / 100`
                : "n/a"}
            </p>
            {selectedMember.churn_risk_explanation && (
              <p style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: -4 }}>
                {selectedMember.churn_risk_explanation}
              </p>
            )}
            {locations && locations.length > 0 && (
              <div className="field" style={{ marginBottom: 12 }}>
                <label style={{ fontSize: 12 }}>Location</label>
                <select
                  value={selectedMember.location_id ?? ""}
                  disabled={savingLocation}
                  onChange={(e) => handleLocationChange(e.target.value)}
                >
                  <option value="">Unassigned</option>
                  {locations.map((loc) => (
                    <option key={loc.id} value={loc.id}>
                      {loc.name}
                    </option>
                  ))}
                </select>
              </div>
            )}
            <h3 style={{ marginBottom: 8 }}>Recommended Rewards</h3>
            {recLoading && <p className="loading">Scoring recommendations...</p>}
            {!recLoading && recommendations && recommendations.length === 0 && (
              <p className="empty">No recommendations available for this member.</p>
            )}
            {!recLoading && recommendations && recommendations.length > 0 && (
              <ul style={{ paddingLeft: 18, margin: 0 }}>
                {recommendations.map((r) => (
                  <li key={r.reward_id} style={{ marginBottom: 8, fontSize: 14 }}>
                    <strong>{r.reward_name}</strong> — {r.points_cost} pts
                    <br />
                    <span style={{ fontSize: 12, color: "#94a3b8" }}>
                      score {r.score.toFixed(2)} &middot; {r.reason}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            <button
              className="primary"
              style={{ marginTop: 16 }}
              onClick={() => setSelectedMember(null)}
            >
              Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
