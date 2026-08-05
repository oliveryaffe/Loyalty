import React, { useEffect, useState } from "react";

import { isApiError } from "../AuthContext";
import {
  BusinessTypeOption,
  DigestStatusOut,
  getBusinessProfile,
  getDigestStatus,
  getNotificationSettings,
  getWinbackRule,
  listBusinessTypes,
  listRewards,
  NotificationSettingsOut,
  resetBusinessProfile,
  RewardOut,
  sendDigestNow,
  updateBusinessProfile,
  updateNotificationSettings,
  updateWinbackRule,
  WinbackRuleOut,
} from "../api/client";
import { formatDateUK } from "../utils";

/**
 * Notification settings (PLAN_BATCH3.md §3): self-serve Slack webhook URL +
 * notification email + on/off toggles for churn-escalation and fraud-alert
 * notifications. No dedicated page/route is specified in the plan text for
 * §3 -- this page exists because the settings are described as "self-serve,
 * no owner dependency", which requires *some* UI for a merchant to
 * actually reach them.
 */
export default function Settings() {
  const [settings, setSettings] = useState<NotificationSettingsOut | null>(null);
  const [slackUrl, setSlackUrl] = useState("");
  const [email, setEmail] = useState("");
  const [notifyChurn, setNotifyChurn] = useState(true);
  const [notifyFraud, setNotifyFraud] = useState(true);
  const [notifyDigest, setNotifyDigest] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const [digestStatus, setDigestStatus] = useState<DigestStatusOut | null>(null);
  const [sendingDigest, setSendingDigest] = useState(false);
  const [digestSentJustNow, setDigestSentJustNow] = useState(false);
  const [digestError, setDigestError] = useState<string | null>(null);

  const [rewards, setRewards] = useState<RewardOut[] | null>(null);
  const [winbackRule, setWinbackRule] = useState<WinbackRuleOut | null>(null);
  const [winbackRewardId, setWinbackRewardId] = useState("");
  const [winbackThreshold, setWinbackThreshold] = useState(65);
  const [savingWinback, setSavingWinback] = useState(false);
  const [winbackSaved, setWinbackSaved] = useState(false);
  const [winbackError, setWinbackError] = useState<string | null>(null);

  const [businessTypes, setBusinessTypes] = useState<BusinessTypeOption[] | null>(null);
  const [businessType, setBusinessType] = useState<string | null>(null);
  const [calibrationSource, setCalibrationSource] = useState<string | null>(null);
  const [savingBusinessType, setSavingBusinessType] = useState(false);
  const [businessTypeError, setBusinessTypeError] = useState<string | null>(null);
  const [businessTypeSaved, setBusinessTypeSaved] = useState(false);
  const [resettingSetup, setResettingSetup] = useState(false);

  function loadBusinessProfile() {
    Promise.all([getBusinessProfile(), listBusinessTypes()])
      .then(([profile, options]) => {
        setBusinessType(profile.business_type);
        setCalibrationSource(profile.calibration_source);
        setBusinessTypes(options);
      })
      .catch((err) =>
        setBusinessTypeError(isApiError(err) ? err.message : "Unable to load business profile.")
      );
  }

  async function handleBusinessTypeChange(value: string) {
    setBusinessTypeError(null);
    setBusinessTypeSaved(false);
    setSavingBusinessType(true);
    try {
      const result = await updateBusinessProfile(value);
      setBusinessType(result.business_type);
      setCalibrationSource(result.calibration_source);
      setBusinessTypeSaved(true);
    } catch (err) {
      setBusinessTypeError(isApiError(err) ? err.message : "Unable to save business type.");
    } finally {
      setSavingBusinessType(false);
    }
  }

  async function handleRestartSetup() {
    setBusinessTypeError(null);
    setResettingSetup(true);
    try {
      await resetBusinessProfile();
      // Full reload (not just local state) so OnboardingModal's mount
      // effect re-fires and picks up business_type=null -- it only
      // checks once per mount, see components/OnboardingModal.tsx.
      window.location.reload();
    } catch (err) {
      setBusinessTypeError(isApiError(err) ? err.message : "Unable to restart setup.");
      setResettingSetup(false);
    }
  }

  function calibrationStatusText(): string | null {
    if (!calibrationSource) return null;
    if (calibrationSource === "calibrated") {
      return "Using your own transaction history to calibrate churn risk and future-value scores.";
    }
    if (calibrationSource === "default_vertical") {
      const label = businessTypes?.find((opt) => opt.value === businessType)?.label ?? "your business type";
      return `Using starting defaults for ${label} until we've seen enough of your own repeat visits to calibrate automatically.`;
    }
    return "Using generic starting defaults until we've seen enough of your own repeat visits to calibrate automatically.";
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
        setNotifyDigest(s.notify_weekly_digest);
      })
      .catch((err) => setError(isApiError(err) ? err.message : "Unable to load notification settings."))
      .finally(() => setLoading(false));
  }

  function loadDigestStatus() {
    getDigestStatus()
      .then(setDigestStatus)
      .catch((err) => setDigestError(isApiError(err) ? err.message : "Unable to load digest status."));
  }

  function loadWinback() {
    Promise.all([listRewards(), getWinbackRule()])
      .then(([rewardList, rule]) => {
        setRewards(rewardList);
        setWinbackRule(rule);
        setWinbackRewardId(rule.reward_id ?? "");
        setWinbackThreshold(rule.churn_risk_threshold);
      })
      .catch((err) => setWinbackError(isApiError(err) ? err.message : "Unable to load win-back suggestion."));
  }

  async function handleSaveWinback(e: React.FormEvent) {
    e.preventDefault();
    if (!winbackRewardId) {
      setWinbackError("Pick a reward to suggest first.");
      return;
    }
    setWinbackError(null);
    setWinbackSaved(false);
    setSavingWinback(true);
    try {
      const result = await updateWinbackRule({
        enabled: true,
        churn_risk_threshold: winbackThreshold,
        reward_id: winbackRewardId,
      });
      setWinbackRule(result);
      setWinbackSaved(true);
    } catch (err) {
      setWinbackError(isApiError(err) ? err.message : "Unable to save win-back suggestion.");
    } finally {
      setSavingWinback(false);
    }
  }

  async function handleSendDigestNow() {
    setDigestError(null);
    setDigestSentJustNow(false);
    setSendingDigest(true);
    try {
      await sendDigestNow();
      setDigestSentJustNow(true);
      loadDigestStatus();
    } catch (err) {
      setDigestError(isApiError(err) ? err.message : "Unable to send digest.");
    } finally {
      setSendingDigest(false);
    }
  }

  useEffect(load, []);
  useEffect(loadBusinessProfile, []);
  useEffect(loadDigestStatus, []);
  useEffect(loadWinback, []);

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
        notify_weekly_digest: notifyDigest,
      });
      setSettings(result);
      setSaved(true);
      loadDigestStatus();
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
        {calibrationStatusText() && (
          <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: 12, marginBottom: 0 }}>
            {calibrationStatusText()}
          </p>
        )}
        {businessTypeSaved && !businessTypeError && (
          <p style={{ color: "var(--mint)", fontSize: 13, marginTop: 8, marginBottom: 0 }}>
            Business type saved.
          </p>
        )}
        <button
          type="button"
          className="secondary"
          style={{ marginTop: 16 }}
          onClick={handleRestartSetup}
          disabled={resettingSetup}
        >
          {resettingSetup ? "Restarting..." : "Restart getting-started setup"}
        </button>
        <p className="hint" style={{ marginTop: 8, marginBottom: 0 }}>
          Clears the business type above and re-shows the getting-started flow next time the dashboard
          loads -- handy for replaying it on this account instead of only ever seeing it once. If this
          account only has sample data (or none at all), the flow will also offer to load a fresh sample
          dataset for whichever business type you pick next. Real uploaded data is never touched.
        </p>
      </div>

      <h2>Notification Settings</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8 }}>
        Get a Slack message and/or email when a customer's churn risk newly escalates to "high", or when a new
        fraud alert is detected. If you've set a suggested win-back reward below, the churn alert includes it
        inline. Fires the next time the Customers or Fraud Alerts page (or a manual re-run) is loaded -- there is
        no background scheduler.
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
            Notify on churn risk escalation (customer newly enters "high" risk)
          </label>
          <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 8, color: "var(--text-secondary)" }}>
            <input type="checkbox" checked={notifyFraud} onChange={(e) => setNotifyFraud(e.target.checked)} />
            Notify on new fraud alerts
          </label>
          <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 8, color: "var(--text-secondary)" }}>
            <input type="checkbox" checked={notifyDigest} onChange={(e) => setNotifyDigest(e.target.checked)} />
            Send me a weekly digest (who's at risk, predicted value, this week's biggest opportunity)
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

      <h2 style={{ marginTop: 32 }}>Win-back Suggestion</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8 }}>
        Pick one reward from your catalog to suggest whenever a customer crosses the churn-risk threshold below --
        it's included inline in the churn alert above and on the Customers page's at-risk filter. This is a
        suggestion only; Ledgerly never grants or sends the reward itself, you comp it in whatever till or loyalty
        tool you already use.
      </p>
      {winbackError && <p className="error-text">{winbackError}</p>}
      {winbackSaved && !winbackError && (
        <p style={{ color: "var(--mint)", fontSize: 13 }}>Win-back suggestion saved.</p>
      )}
      {rewards === null ? (
        <p className="loading">Loading rewards...</p>
      ) : rewards.length === 0 ? (
        <div className="card" style={{ maxWidth: 520 }}>
          <p className="hint" style={{ marginTop: 0, marginBottom: 0 }}>
            No rewards in your catalog yet -- add one on the Rewards page first, then come back here to suggest it
            for at-risk customers.
          </p>
        </div>
      ) : (
        <form onSubmit={handleSaveWinback} className="card" style={{ maxWidth: 520, display: "grid", gap: 14 }}>
          <div className="field">
            <label>Suggested reward</label>
            <select value={winbackRewardId} onChange={(e) => setWinbackRewardId(e.target.value)}>
              <option value="" disabled>
                Select a reward...
              </option>
              {rewards.filter((r) => r.active).map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Churn risk threshold ({winbackThreshold})</label>
            <input
              type="range"
              min={0}
              max={100}
              step={5}
              value={winbackThreshold}
              onChange={(e) => setWinbackThreshold(Number(e.target.value))}
            />
          </div>
          <div>
            <button className="primary" style={{ width: "auto", padding: "8px 20px" }} type="submit" disabled={savingWinback}>
              {savingWinback ? "Saving..." : "Save suggestion"}
            </button>
          </div>
          {winbackRule?.reward_id && (
            <p className="hint" style={{ marginTop: 0 }}>
              Currently suggesting "{rewards.find((r) => r.id === winbackRule.reward_id)?.name ?? "a reward"}" for
              customers at or above {winbackRule.churn_risk_threshold} churn risk.
            </p>
          )}
        </form>
      )}

      <h2 style={{ marginTop: 32 }}>Weekly Digest</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: -8 }}>
        A short summary -- who's at risk, what your current customers are worth going forward, and this
        week's biggest opportunity -- sent to whichever Slack/email channel is configured above. Fires
        automatically once a week (no login required) once turned on above; use the button below to send
        yourself a preview any time.
      </p>
      {digestError && <p className="error-text">{digestError}</p>}
      <div className="card" style={{ maxWidth: 520 }}>
        {digestStatus ? (
          <>
            <p style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 0 }}>
              Status: {digestStatus.enabled ? "on" : "off"}
              {digestStatus.enabled && !digestStatus.has_notification_channel && " -- add a Slack URL or email above to actually receive it"}
            </p>
            <p style={{ fontSize: 13, color: "var(--text-secondary)" }}>
              Last sent: {digestStatus.last_digest_sent_at ? formatDateUK(digestStatus.last_digest_sent_at) : "never"}
            </p>
          </>
        ) : (
          <p className="loading">Loading digest status...</p>
        )}
        <button
          type="button"
          className="secondary"
          onClick={handleSendDigestNow}
          disabled={sendingDigest}
        >
          {sendingDigest ? "Sending..." : "Send digest now"}
        </button>
        {digestSentJustNow && !digestError && (
          <p style={{ color: "var(--mint)", fontSize: 13, marginTop: 8, marginBottom: 0 }}>
            Digest sent{digestStatus && !digestStatus.has_notification_channel ? " (computed, but no Slack/email configured to deliver it to)" : ""}.
          </p>
        )}
      </div>
    </div>
  );
}
