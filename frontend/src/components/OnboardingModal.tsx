import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  BusinessTypeOption,
  getBusinessProfile,
  listBusinessTypes,
  updateBusinessProfile,
} from "../api/client";
import { isApiError } from "../AuthContext";

type Step = "business_type" | "get_started";

/**
 * Two-step getting-started flow, shown once on first dashboard load
 * whenever the merchant hasn't set a business_type yet (including via
 * "Other", a valid explicit answer) -- or any time it's been reset via
 * Settings > "Restart getting-started setup".
 *
 * Step 1 (business type) isn't cosmetic -- it feeds the AI layer's
 * calibration fallback (see backend/app/ai/churn_model.py::
 * BUSINESS_TYPE_CALIBRATIONS): until a merchant has enough of their own
 * transaction history for real auto-calibration, churn risk and
 * future-value scoring use a vertical-appropriate starting point instead
 * of always defaulting to coffee-shop-tuned thresholds.
 *
 * Step 2 (get started) is a lightweight nudge, not a data requirement --
 * closing it without uploading anything is a fully supported path (the
 * pre-loaded demo data, or an empty account, both work fine).
 */
export default function OnboardingModal() {
  const navigate = useNavigate();
  const [step, setStep] = useState<Step>("business_type");
  const [visible, setVisible] = useState(false);
  const [options, setOptions] = useState<BusinessTypeOption[] | null>(null);
  const [chosenLabel, setChosenLabel] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getBusinessProfile(), listBusinessTypes()])
      .then(([profile, businessTypes]) => {
        setOptions(businessTypes);
        if (profile.business_type === null) {
          setStep("business_type");
          setVisible(true);
        }
      })
      .catch(() => {
        // Not fatal -- if this fails to load, the merchant just doesn't
        // see the picker this session; scoring still works fine off
        // DEFAULT_CALIBRATION in the meantime.
      });
  }, []);

  async function choose(value: string, label: string) {
    setError(null);
    setSaving(value);
    try {
      await updateBusinessProfile(value);
      setChosenLabel(label);
      setStep("get_started");
    } catch (err) {
      setError(isApiError(err) ? err.message : "Unable to save -- please try again.");
    } finally {
      setSaving(null);
    }
  }

  function finish(goToInsights: boolean) {
    setVisible(false);
    if (goToInsights) {
      navigate("/insights");
    }
  }

  if (!visible || !options) return null;

  return (
    <div
      role="dialog"
      aria-label={step === "business_type" ? "What kind of business is this?" : "Get started"}
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
        {step === "business_type" ? (
          <>
            <h1>What kind of business is this?</h1>
            <p className="subtitle">
              Helps us set sensible starting points for churn risk and future-value forecasts until we've
              seen enough of your own transaction history to calibrate automatically. You can change this
              later in Settings.
            </p>
            {error && <p className="error-text">{error}</p>}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              {options.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  className="secondary"
                  disabled={saving !== null}
                  onClick={() => choose(opt.value, opt.label)}
                  style={{ padding: "12px 14px", textAlign: "left" }}
                >
                  {saving === opt.value ? "Saving..." : opt.label}
                </button>
              ))}
            </div>
          </>
        ) : (
          <>
            <h1>You're set up{chosenLabel ? ` as a ${chosenLabel.toLowerCase()}` : ""}.</h1>
            <p className="subtitle">
              Next, bring in your own transaction history so churn risk and future-value scoring can
              calibrate to your real customers instead of starting defaults. You can always do this later
              from the Insights page.
            </p>
            {error && <p className="error-text">{error}</p>}
            <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 8 }}>
              <button type="button" className="primary" onClick={() => finish(true)}>
                Upload my customer data now
              </button>
              <button type="button" className="secondary" onClick={() => finish(false)}>
                I'll do this later -- let me explore first
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
