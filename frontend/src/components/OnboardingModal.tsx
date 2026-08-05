import React, { useEffect, useState } from "react";

import {
  BusinessTypeOption,
  getBusinessProfile,
  listBusinessTypes,
  updateBusinessProfile,
} from "../api/client";
import { isApiError } from "../AuthContext";

/**
 * One-question onboarding: which kind of business is this. Shown once,
 * on first dashboard load, whenever the merchant hasn't set a
 * business_type yet (including via "Other", a valid explicit answer).
 *
 * This isn't cosmetic -- it feeds the AI layer's calibration fallback
 * (see backend/app/ai/churn_model.py::BUSINESS_TYPE_CALIBRATIONS): until
 * a merchant has enough of their own transaction history for real
 * auto-calibration, churn risk and future-value scoring use a
 * vertical-appropriate starting point instead of always defaulting to
 * coffee-shop-tuned thresholds.
 */
export default function OnboardingModal() {
  const [visible, setVisible] = useState(false);
  const [options, setOptions] = useState<BusinessTypeOption[] | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getBusinessProfile(), listBusinessTypes()])
      .then(([profile, businessTypes]) => {
        setOptions(businessTypes);
        if (profile.business_type === null) {
          setVisible(true);
        }
      })
      .catch(() => {
        // Not fatal -- if this fails to load, the merchant just doesn't
        // see the picker this session; scoring still works fine off
        // DEFAULT_CALIBRATION in the meantime.
      });
  }, []);

  async function choose(value: string) {
    setError(null);
    setSaving(value);
    try {
      await updateBusinessProfile(value);
      setVisible(false);
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to save -- please try again.");
    } finally {
      setSaving(null);
    }
  }

  if (!visible || !options) return null;

  return (
    <div
      role="dialog"
      aria-label="What kind of business is this?"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(11,14,20,0.75)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 20,
      }}
    >
      <div className="login-card" style={{ width: 480 }}>
        <h1>What kind of business is this?</h1>
        <p className="subtitle">
          Helps us set sensible starting points for churn risk and future-value forecasts until we've seen
          enough of your own transaction history to calibrate automatically. You can change this later in
          Settings.
        </p>
        {error && <p className="error-text">{error}</p>}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          {options.map((opt) => (
            <button
              key={opt.value}
              type="button"
              className="secondary"
              disabled={saving !== null}
              onClick={() => choose(opt.value)}
              style={{ padding: "12px 14px", textAlign: "left" }}
            >
              {saving === opt.value ? "Saving..." : opt.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
