import React, { useEffect, useState } from "react";

import { isApiError } from "../AuthContext";
import {
  gdprEraseMember,
  gdprExportMember,
  GdprAuditLogEntryOut,
  getGdprAuditLog,
  getGdprSummary,
  GdprSummaryOut,
  listMembers,
  MemberWithChurn,
} from "../api/client";
import { formatDateUK } from "../utils";

function downloadJson(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/**
 * Compliance tab: the operational side of UK GDPR subject rights that
 * previously existed only as raw API endpoints (POST /members/{id}/gdpr-
 * erase, GET /members/{id}/gdpr-export) with zero UI. Two things live
 * here: a search-and-act panel for handling an actual subject access/
 * erasure request, and a chronological audit log of every export/erasure
 * that's happened on this account (app/api/gdpr.py) -- "show your work",
 * not just "have the capability".
 */
export default function Compliance() {
  const [summary, setSummary] = useState<GdprSummaryOut | null>(null);
  const [auditLog, setAuditLog] = useState<GdprAuditLogEntryOut[] | null>(null);
  const [members, setMembers] = useState<MemberWithChurn[] | null>(null);
  const [search, setSearch] = useState("");
  const [error, setError] = useState<string | null>(null);

  const [exportingId, setExportingId] = useState<string | null>(null);
  const [confirmingEraseId, setConfirmingEraseId] = useState<string | null>(null);
  const [erasingId, setErasingId] = useState<string | null>(null);

  function load() {
    Promise.all([getGdprSummary(), getGdprAuditLog(), listMembers(false)])
      .then(([s, log, m]) => {
        setSummary(s);
        setAuditLog(log);
        setMembers(m);
      })
      .catch((err) => setError(isApiError(err) ? err.message : "Unable to load compliance data."));
  }

  useEffect(load, []);

  async function handleExport(member: MemberWithChurn) {
    setError(null);
    setExportingId(member.id);
    try {
      const data = await gdprExportMember(member.id);
      downloadJson(`ledgerly-subject-export-${member.id}.json`, data);
      const log = await getGdprAuditLog();
      setAuditLog(log);
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to export this member's data.");
    } finally {
      setExportingId(null);
    }
  }

  async function handleErase(member: MemberWithChurn) {
    setError(null);
    setErasingId(member.id);
    try {
      await gdprEraseMember(member.id);
      setConfirmingEraseId(null);
      load();
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to erase this member's data.");
    } finally {
      setErasingId(null);
    }
  }

  const filteredMembers = (members ?? []).filter((m) => {
    if (m.erased_at) return false; // already erased -- nothing left to act on
    const q = search.trim().toLowerCase();
    if (!q) return false; // don't show the whole member list by default -- search-first
    return `${m.first_name} ${m.last_name}`.toLowerCase().includes(q) || m.email.toLowerCase().includes(q);
  });

  return (
    <div>
      <h2>Compliance</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8, maxWidth: 680 }}>
        Handle UK GDPR subject access (Art. 15/20) and erasure (Art. 17) requests, and see a record of
        every request that's been actioned on this account. This covers customer data only -- Ledgerly
        isn't a lawyer, and this isn't legal advice on your obligations.
      </p>
      {error && <p className="error-text">{error}</p>}

      {summary && (
        <div className="card-grid" style={{ marginBottom: 32 }}>
          <div className="card">
            <div className="label">Customers</div>
            <div className="value">{summary.total_members}</div>
          </div>
          <div className="card">
            <div className="label">Erased customers</div>
            <div className="value">{summary.erased_members}</div>
          </div>
          <div className="card">
            <div className="label">Requests (last 30 days)</div>
            <div className="value">{summary.requests_last_30_days}</div>
          </div>
        </div>
      )}

      <h3>Handle a subject request</h3>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -6 }}>
        Search for the member who made the request, then export their data (subject access) or erase it
        (right to erasure). Erasure anonymises the member and cannot be undone.
      </p>
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <input
          className="pill-select"
          style={{ minWidth: 300 }}
          placeholder="Search by name or email..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      {search.trim() && (
        filteredMembers.length === 0 ? (
          <p className="empty">No matching members.</p>
        ) : (
          <table style={{ marginBottom: 32 }}>
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredMembers.map((m) => (
                <tr key={m.id}>
                  <td>
                    {m.first_name} {m.last_name}
                  </td>
                  <td>{m.email}</td>
                  <td style={{ display: "flex", gap: 8 }}>
                    <button
                      className="secondary"
                      onClick={() => handleExport(m)}
                      disabled={exportingId === m.id}
                    >
                      {exportingId === m.id ? "Exporting..." : "Export data"}
                    </button>
                    <button className="secondary" onClick={() => setConfirmingEraseId(m.id)}>
                      Erase data
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      )}

      <h3>Audit log</h3>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -6 }}>
        Every export and erasure actioned on this account, most recent first.
      </p>
      {auditLog === null ? (
        <p className="loading">Loading audit log...</p>
      ) : auditLog.length === 0 ? (
        <p className="empty">No subject requests have been actioned yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Action</th>
              <th>Customer</th>
              <th>Performed by</th>
              <th>Date</th>
            </tr>
          </thead>
          <tbody>
            {auditLog.map((entry) => (
              <tr key={entry.id}>
                <td style={{ textTransform: "capitalize" }}>{entry.action}</td>
                <td>{entry.member_label}</td>
                <td>{entry.performed_by_email}</td>
                <td>{formatDateUK(entry.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {confirmingEraseId && (
        <div
          role="dialog"
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(11,14,20,0.75)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 20,
          }}
          onClick={() => setConfirmingEraseId(null)}
        >
          <div className="login-card modal-card" style={{ width: 420 }} onClick={(e) => e.stopPropagation()}>
            <h1>Erase this member?</h1>
            <p className="subtitle">
              This overwrites their name and email with anonymised placeholders. Their transaction history
              stays for your own business records, but it can no longer be tied back to a real person. This
              cannot be undone.
            </p>
            <div style={{ display: "flex", gap: 10, marginTop: 8 }}>
              <button
                className="primary"
                style={{ background: "var(--coral)" }}
                onClick={() => {
                  const member = members?.find((m) => m.id === confirmingEraseId);
                  if (member) handleErase(member);
                }}
                disabled={erasingId !== null}
              >
                {erasingId ? "Erasing..." : "Yes, erase permanently"}
              </button>
              <button className="secondary" onClick={() => setConfirmingEraseId(null)} disabled={erasingId !== null}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
