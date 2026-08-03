import React, { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { isApiError, useAuth } from "../AuthContext";

export default function Login() {
  const { login, isAuthenticated } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [email, setEmail] = useState("demo@merchant.com");
  const [password, setPassword] = useState("demo1234");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (isAuthenticated) {
    const redirectTo = (location.state as { from?: string } | null)?.from ?? "/";
    return <Navigate to={redirectTo} replace />;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      navigate("/", { replace: true });
    } catch (err) {
      setError(
        isApiError(err) ? err.message : "Unable to log in. Please try again."
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="sidebar-brand" style={{ marginBottom: 16 }}>
          <div className="brand-mark">L</div>
          <span className="brand-word">Ledgerly</span>
        </div>
        <h1>Merchant Dashboard</h1>
        <p className="subtitle">Sign in to manage your loyalty program</p>

        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="username"
            />
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
            />
          </div>
          <button className="primary" type="submit" disabled={submitting}>
            {submitting ? "Logging in..." : "Log in"}
          </button>
          {error && <p className="error-text">{error}</p>}
        </form>

        <p className="hint">
          Demo credentials are pre-filled. Run <code>python scripts/seed_data.py</code>{" "}
          in <code>backend/</code> first to create the demo merchant account and
          synthetic data.
        </p>
      </div>
    </div>
  );
}
