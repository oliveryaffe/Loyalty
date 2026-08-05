import React, { useEffect, useMemo, useRef, useState } from "react";

import { isApiError } from "../AuthContext";
import {
  AudienceExportFormat,
  downloadInsightsReport,
  downloadNextBestAudienceExport,
  FutureValueOut,
  getFutureValue,
  getNextBestProduct,
  InsightsUploadResult,
  listMembers,
  NextBestOut,
  uploadInsightsCsv,
} from "../api/client";
import { formatGBP } from "../utils";

type SortKey = "name" | "tier" | "predicted_future_value";

const HORIZON_DAYS = 90;
const NEXT_BEST_CONCURRENCY = 8; // bounded-concurrency background fetch -- see loadNextBest()

function ModelBadge({ modelUsed }: { modelUsed: "trained" | "heuristic" }) {
  return (
    <span className={`badge badge-${modelUsed}`} title={
      modelUsed === "trained"
        ? "Backtested Ridge regression, trained on this merchant's own historical spend."
        : "Heuristic estimate (avg order value x purchase frequency x retention adjustment) -- used when there isn't enough pre-cutoff history to train on."
    }>
      {modelUsed}
    </span>
  );
}

export default function Insights() {
  const [futureValues, setFutureValues] = useState<FutureValueOut[] | null>(null);
  // FutureValueOut (see PLAN_BATCH2.md §5's literal schema) doesn't carry
  // tier -- merged in separately from the existing /members endpoint so
  // the Tier column/sort the plan's frontend section calls for is real
  // data, not guessed.
  const [tierByMemberId, setTierByMemberId] = useState<Record<string, string>>({});
  const [nextBest, setNextBest] = useState<Record<string, NextBestOut[] | "loading" | "error">>({});
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("predicted_future_value");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<InsightsUploadResult | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [mintPoints, setMintPoints] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const [exportCategory, setExportCategory] = useState("");
  const [exportFormat, setExportFormat] = useState<AudienceExportFormat>("generic");
  const [exportingAudience, setExportingAudience] = useState(false);
  const [exportAudienceError, setExportAudienceError] = useState<string | null>(null);

  function loadFutureValues() {
    setFutureValues(null);
    Promise.all([getFutureValue(HORIZON_DAYS), listMembers(false)])
      .then(([rows, members]) => {
        setFutureValues(rows);
        setTierByMemberId(Object.fromEntries(members.map((m) => [m.id, m.tier])));
        loadNextBest(rows.map((r) => r.member_id));
      })
      .catch((err) => setError(String(err)));
  }

  // Next-best-product only has a per-member endpoint (no merchant-wide
  // bulk list, unlike future-value/churn) -- fetch it in the background
  // with bounded concurrency so the table renders immediately with
  // future-value data and next-best columns fill in progressively instead
  // of blocking on ~620 sequential requests.
  function loadNextBest(memberIds: string[]) {
    setNextBest((prev) => {
      const next = { ...prev };
      memberIds.forEach((id) => {
        if (!next[id]) next[id] = "loading";
      });
      return next;
    });

    let cursor = 0;
    async function worker() {
      while (cursor < memberIds.length) {
        const id = memberIds[cursor];
        cursor += 1;
        try {
          const result = await getNextBestProduct(id, 3);
          setNextBest((prev) => ({ ...prev, [id]: result }));
        } catch {
          setNextBest((prev) => ({ ...prev, [id]: "error" }));
        }
      }
    }
    const workers = Array.from({ length: NEXT_BEST_CONCURRENCY }, () => worker());
    void Promise.all(workers);
  }

  useEffect(loadFutureValues, []);

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  const filtered = useMemo(() => {
    if (!futureValues) return [];
    const q = search.trim().toLowerCase();
    let rows = futureValues;
    if (q) {
      rows = rows.filter((r) => `${r.first_name} ${r.last_name}`.toLowerCase().includes(q));
    }
    const sorted = [...rows].sort((a, b) => {
      let av: number | string;
      let bv: number | string;
      switch (sortKey) {
        case "name":
          av = `${a.first_name} ${a.last_name}`;
          bv = `${b.first_name} ${b.last_name}`;
          break;
        case "predicted_future_value":
          av = a.predicted_future_value;
          bv = b.predicted_future_value;
          break;
        case "tier":
        default:
          av = tierByMemberId[a.member_id] ?? "";
          bv = tierByMemberId[b.member_id] ?? "";
          break;
      }
      if (av < bv) return sortDir === "asc" ? -1 : 1;
      if (av > bv) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
    return sorted;
  }, [futureValues, tierByMemberId, search, sortKey, sortDir]);

  function onUploadClick() {
    fileInputRef.current?.click();
  }

  function onFileSelected(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file later
    if (!file) return;

    setUploading(true);
    setUploadError(null);
    setUploadResult(null);
    uploadInsightsCsv(file, mintPoints)
      .then((result) => {
        setUploadResult(result);
        loadFutureValues();
      })
      .catch((err) => setUploadError(String(err)))
      .finally(() => setUploading(false));
  }

  function onDownloadClick() {
    setDownloading(true);
    downloadInsightsReport(HORIZON_DAYS)
      .catch((err) => setError(String(err)))
      .finally(() => setDownloading(false));
  }

  // Distinct #1-suggestion categories seen so far across loaded members --
  // populates the "export by next-best category" picker below. Grows in
  // as loadNextBest's background workers fill in, same as the table cells.
  const nextBestCategories = useMemo(() => {
    const seen = new Set<string>();
    Object.values(nextBest).forEach((v) => {
      if (Array.isArray(v) && v[0]) seen.add(v[0].category);
    });
    return Array.from(seen).sort();
  }, [nextBest]);

  async function handleExportByCategory() {
    if (!exportCategory) return;
    setExportAudienceError(null);
    setExportingAudience(true);
    try {
      await downloadNextBestAudienceExport(exportCategory, exportFormat);
    } catch (err) {
      setExportAudienceError(isApiError(err) ? err.message : "Unable to export this audience.");
    } finally {
      setExportingAudience(false);
    }
  }

  return (
    <div>
      <h2>Insights</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8 }}>
        Predicted future value (90-day) and next-best-category/product per member.
      </p>
      {error && <p className="error-text">{error}</p>}

      <div className="toolbar">
        <input
          className="pill-select"
          style={{ minWidth: 260 }}
          placeholder="Search by name..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 6, color: "var(--text-secondary)" }}>
            <input
              type="checkbox"
              checked={mintPoints}
              onChange={(e) => setMintPoints(e.target.checked)}
            />
            Mint points for upload
          </label>
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,text/csv"
            style={{ display: "none" }}
            onChange={onFileSelected}
          />
          <button className="secondary" onClick={onUploadClick} disabled={uploading}>
            {uploading ? "Uploading..." : "Upload CSV"}
          </button>
          <button className="secondary" onClick={onDownloadClick} disabled={downloading}>
            {downloading ? "Preparing..." : "Download report"}
          </button>
        </div>
      </div>

      <details style={{ marginBottom: 16, fontSize: 13, color: "var(--text-secondary)" }}>
        <summary style={{ cursor: "pointer", color: "var(--text-primary)" }}>
          What columns does my CSV need?
        </summary>
        <div style={{ marginTop: 8, lineHeight: 1.6 }}>
          <p style={{ margin: "0 0 6px" }}>
            <strong>Required:</strong> <code>customer_email</code>, <code>transaction_date</code>,{" "}
            <code>amount_gbp</code>.
          </p>
          <p style={{ margin: 0 }}>
            <strong>Optional:</strong> <code>customer_first_name</code> / <code>customer_last_name</code> (a
            member is labelled "Unknown Customer" if omitted), <code>product_category</code> /{" "}
            <code>product_name</code> (improves next-best-product quality), <code>channel</code> (pos,
            online, or mobile), and <code>external_order_id</code>. Customers are matched to existing
            members by email automatically. Include <code>external_order_id</code> if you want to
            re-upload a file safely -- rows with an order ID already on file are skipped as duplicates;
            without one, re-uploading the same rows will add them again.
          </p>
        </div>
      </details>

      {nextBestCategories.length > 0 && (
        <div className="toolbar" style={{ marginBottom: 16 }}>
          <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
            Export everyone whose top suggestion is:
          </span>
          <select
            className="pill-select"
            value={exportCategory}
            onChange={(e) => setExportCategory(e.target.value)}
            aria-label="Next-best category to export"
          >
            <option value="">Choose a category...</option>
            {nextBestCategories.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
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
          <button
            type="button"
            className="secondary"
            onClick={handleExportByCategory}
            disabled={!exportCategory || exportingAudience}
          >
            {exportingAudience ? "Exporting..." : "Export audience"}
          </button>
        </div>
      )}
      {exportAudienceError && <p className="error-text">{exportAudienceError}</p>}

      {uploadError && <p className="error-text">Upload failed: {uploadError}</p>}
      {uploadResult && (
        <div className="upload-banner">
          <strong>Upload complete.</strong> {uploadResult.rows_ingested} ingested,{" "}
          {uploadResult.rows_skipped_duplicate} duplicate,{" "}
          {uploadResult.rows_failed} failed, {uploadResult.members_created} new member(s) created.
          {uploadResult.errors.length > 0 && (
            <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
              {uploadResult.errors.slice(0, 5).map((e, i) => (
                <li key={i} className="error-row">
                  row {e.row}: {e.reason}
                </li>
              ))}
              {uploadResult.errors.length > 5 && (
                <li className="error-row">...and {uploadResult.errors.length - 5} more</li>
              )}
            </ul>
          )}
        </div>
      )}

      {futureValues === null ? (
        <p className="loading">Loading insights...</p>
      ) : filtered.length === 0 ? (
        <p className="empty">No members match your search.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th onClick={() => toggleSort("name")}>Name</th>
              <th onClick={() => toggleSort("tier")}>Tier</th>
              <th onClick={() => toggleSort("predicted_future_value")}>
                Predicted Future Value <span style={{ fontWeight: 400, textTransform: "none" }}>(90-day)</span>
              </th>
              <th>Model</th>
              <th>Next Best Category</th>
              <th
                title="Ranked suggestions, most confident first. Upload transaction data with product detail to unlock named products instead of just categories."
              >
                Top Suggestions
              </th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => {
              const nb = nextBest[r.member_id];
              const top = Array.isArray(nb) ? nb[0] : undefined;
              return (
                <tr key={r.member_id}>
                  <td>
                    {r.first_name} {r.last_name}
                  </td>
                  <td>{tierByMemberId[r.member_id] ?? "—"}</td>
                  <td>{formatGBP(r.predicted_future_value)}</td>
                  <td>
                    <ModelBadge modelUsed={r.model_used} />
                  </td>
                  <td>
                    {nb === "loading" ? (
                      <span className="loading" style={{ padding: 0 }}>...</span>
                    ) : nb === "error" || !top ? (
                      "—"
                    ) : (
                      top.category
                    )}
                  </td>
                  <td
                    title={
                      top && top.data_granularity === "category"
                        ? "Upload transaction data with product detail to unlock named products instead of just categories"
                        : undefined
                    }
                  >
                    {nb === "loading" ? (
                      <span className="loading" style={{ padding: 0 }}>...</span>
                    ) : nb === "error" || !Array.isArray(nb) || nb.length === 0 ? (
                      "—"
                    ) : (
                      <ol style={{ margin: 0, paddingLeft: 16, fontSize: 12 }}>
                        {nb.map((suggestion, i) => (
                          <li key={i}>
                            {suggestion.product_name ?? suggestion.category}
                            {suggestion.product_name && (
                              <span style={{ color: "var(--text-secondary)" }}> ({suggestion.category})</span>
                            )}
                          </li>
                        ))}
                      </ol>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
