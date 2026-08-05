import React, { useEffect, useState } from "react";

import { isApiError } from "../AuthContext";
import {
  BusinessTypeOption,
  getBusinessProfile,
  getNotificationSettings,
  listBusinessTypes,
  NotificationSettingsOut,
  updateBusinessProfile,
  updateNotificationSettings,
} from "../api/client";

/**
 * Notification settings (PLAN_BATCH3.md §3): self-serve Slack webhook URL +
 * notification email + on/off toggles for churn-escalation and fraud-alert
 * notifications. No dedicated page/route is specified in the plan text for
 * §3 (unlike §4's Winback.tsx, which the plan calls out explicitly) -- this
 * page exists because the settings are described as "self-serve, no owner
 * dependency", which requires *some* UI for a merchant to actually reach
 * them.
 */
export default function Settings() {
  const [settings, setSettings] = useState<NotificationSettingsOut | null>(null);
  const [slackUrl, setSlackUrl] = useState("");
  const [email, setEmail] = useState("");
  const [notifyChurn, setNotifyChurn] = useState(true);
  const [notifyFraud, setNotifyFraud] = useState(true);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const [businessTypes, setBusinessTypes] = useState<BusinessTypeOption[] | null>(null);
  const [businessType, setBusinessType] = useState<string | null>(null);
  const [savingBusinessType, setSavingBusinessType] = useState(false);
  const [businessTypeError, setBusinessTypeError] = useState<string | null>(null);

  function loadBusinessProfile() {
    Promise.all([getBusinessProfile(), listBusinessTypes()])
      .then(([profile, options]) => {
        setBusinessType(profile.business_type);
        setBusinessTypes(options);
      })
      .catch((err) =>
        setBusinessTypeError(isApiError(err) ? err.message : "Unable to load business profile.")
      );
  }

  async function handleBusinessTypeChange(value: string) {
    setBusinessTypeError(null);
    setSavingBusinessType(true);
    try {
      const result = await updateBusinessProfile(value);
      setBusinessType(result.business_type);
    } catch (err) {
      setBusinessTypeError(isApiError(err) ? err.message : "Unable to save business type.");
    } finally {
      setSavingBusinessType(false);
    }
  }

  function load() {
    setLoading(true);
    getNotificationSettings()
      .then((s) => {
        setSettings(s);
        setSlackUrl(s.notification_slack_webhook_url ?? "");
        setEmail(s.notification_email ?? "");
        setNotifyChurn(s.notify_on_churn_risk);
        setNotifyFraud(s.notify_on_fraud_alert);
      })
      .catch((err) => setError(isApiError(err) ? err.message : "Unable to load notification settings."))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);
  useEffect(loadBusinessProfile, []);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSaved(false);
    setSaving(true);
    try {
      const result = await updateNotificationSettings({
        notification_slack_webhook_url: slackUrl.trim() === "" ? null : slackUrl.trim(),
        notification_email: email.trim() === "" ? null : email.trim(),
        notify_on_churn_risk: notifyChurn,
        notify_on_fraud_alert: notifyFraud,
      });
      setSettings(result);
      setSaved(true);
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to save notification settings.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <h2>Business Profile</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8 }}>
        Sets a sensible starting point for churn risk and future-value forecasts until we've seen enough of
        your own transaction history to calibrate automatically -- see the Insights page for how that plays
        out once it kicks in.
      </p>
      {businessTypeError && <p className="error-text">{businessTypeError}</p>}
      <div className="card" style={{ maxWidth: 520, marginBottom: 32 }}>
        <div className="field">
          <label>Business type</label>
          <select
            value={businessType ?? ""}
            disabled={savingBusinessType || !businessTypes}
            onChange={(e) => handleBusinessTypeChange(e.target.value)}
          >
            <option value="" disabled>
              Select...
            </option>
            {(businessTypes ?? []).map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <h2>Notification Settings</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8 }}>
        Get a Slack message and/or email when a member's churn risk newly escalates to "high", or when a new
        fraud alert is detected. Fires the next time the Members or Fraud Alerts page (or a manual re-run) is
        loaded -- there is no background scheduler.
      </p>
      {error && <p className="error-text">{error}</p>}
      {saved && !error && (
        <p style={{ color: "var(--mint)", fontSize: 13 }}>Settings saved.</p>
      )}

      {loading ? (
        <p className="loading">Loading settings...</p>
      ) : (
        <form onSubmit={handleSave} className="card" style={{ maxWidth: 520, display: "grid", gap: 14 }}>
          <div className="field">
            <label>Slack incoming-webhook URL</label>
            <input
              type="url"
              placeholder="https://hooks.slack.com/services/..."
              value={slackUrl}
              onChange={(e) => setSlackUrl(e.target.value)}
            />
          </div>
          <div className="field">
            <label>Notification email</label>
            <input
              type="email"
              placeholder="alerts@yourbusiness.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 8, color: "var(--text-secondary)" }}>
            <input type="checkbox" checked={notifyChurn} onChange={(e) => setNotifyChurn(e.target.checked)} />
            Notify on churn risk escalation (member newly enters "high" risk)
          </label>
          <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 8, color: "var(--text-secondary)" }}>
            <input type="checkbox" checked={notifyFraud} onChange={(e) => setNotifyFraud(e.target.checked)} />
            Notify on new fraud alerts
          </label>
          <div>
            <button className="primary" style={{ width: "auto", padding: "8px 20px" }} type="submit" disabled={saving}>
              {saving ? "Saving..." : "Save settings"}
            </button>
          </div>
          <p className="hint" style={{ marginTop: 0 }}>
            Slack webhooks are self-serve -- generate one from your workspace's "Incoming Webhooks" app and paste
            the URL above. Email delivery depends on Ledgerly's platform having SMTP configured by the site owner;
            if it isn't, email notifications are silently skipped (logged, not broken) while Slack keeps working.
          </p>
          {settings && !settings.notification_slack_webhook_url && !settings.notification_email && (
            <p className="hint" style={{ marginTop: 0 }}>
              No delivery method configured yet -- notifications are computed but not sent until you add a Slack
              URL and/or email above.
            </p>
          )}
        </form>
      )}
    </div>
  );
}
