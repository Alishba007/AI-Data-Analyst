import { useState } from "react";
import { login, signup } from "../api";

/**
 * Decorative background: original bar chart, line chart, and spreadsheet
 * grid artwork drawn in the app's own ledger palette — not a stock photo,
 * so it stays consistent with the rest of the product instead of looking
 * like a bolted-on template.
 */
function AnalystBackdrop() {
  return (
    <svg
      className="login__backdrop"
      viewBox="0 0 1000 700"
      preserveAspectRatio="xMidYMid slice"
      aria-hidden="true"
    >
      {/* Spreadsheet-style cell grid, faint, across the whole canvas */}
      <g stroke="var(--color-ledger-line)" strokeWidth="1" opacity="0.6">
        {Array.from({ length: 15 }).map((_, i) => (
          <line key={`v${i}`} x1={i * 70} y1="0" x2={i * 70} y2="700" />
        ))}
        {Array.from({ length: 11 }).map((_, i) => (
          <line key={`h${i}`} x1="0" y1={i * 70} x2="1000" y2={i * 70} />
        ))}
      </g>

      {/* Bar chart, bottom-left */}
      <g opacity="0.5">
        {[60, 100, 75, 130, 95, 150].map((h, i) => (
          <rect
            key={i}
            x={70 + i * 46}
            y={560 - h}
            width="30"
            height={h}
            fill="var(--color-accent-teal)"
            rx="2"
          />
        ))}
      </g>

      {/* Line chart, top-right, with dots at each point */}
      <g opacity="0.55">
        <polyline
          points="620,180 690,230 750,140 820,190 880,110 940,150"
          fill="none"
          stroke="var(--color-accent-amber)"
          strokeWidth="4"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        {[
          [620, 180], [690, 230], [750, 140], [820, 190], [880, 110], [940, 150],
        ].map(([cx, cy], i) => (
          <circle key={i} cx={cx} cy={cy} r="6" fill="var(--color-accent-amber)" />
        ))}
      </g>

      {/* A second, smaller bar cluster, top-left, for balance */}
      <g opacity="0.35">
        {[40, 70, 55].map((h, i) => (
          <rect
            key={i}
            x={90 + i * 34}
            y={130 - h}
            width="22"
            height={h}
            fill="var(--color-ink-soft)"
            rx="2"
          />
        ))}
      </g>

      {/* Scattered pie/donut motif, bottom-right */}
      <g opacity="0.45" transform="translate(870,560)">
        <circle r="60" fill="none" stroke="var(--color-accent-teal)" strokeWidth="16" />
        <circle
          r="60"
          fill="none"
          stroke="var(--color-accent-amber)"
          strokeWidth="16"
          strokeDasharray="110 377"
          transform="rotate(-90)"
        />
      </g>
    </svg>
  );
}

export default function Login({ onAuthenticated }) {
  const [mode, setMode] = useState("login"); // login | signup
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      const action = mode === "login" ? login : signup;
      const data = await action(email, password);
      onAuthenticated(data.email);
    } catch (err) {
      const detail = err?.response?.data?.detail;
      setError(Array.isArray(detail) ? detail[0]?.msg : detail || "Something went wrong.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login">
      <AnalystBackdrop />

      <div className="login__card">
        <div className="login__badge">AI DATA ANALYST</div>
        <h1 className="login__title">{mode === "login" ? "Sign In." : "Create Account."}</h1>
        <p className="login__subtitle">
          {mode === "login"
            ? "Your data, your queries, your ledger."
            : "Start turning questions into queries."}
        </p>

        <form onSubmit={handleSubmit} className="login__form">
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="Email"
            required
            autoComplete="email"
          />
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password"
            required
            minLength={8}
            autoComplete={mode === "login" ? "current-password" : "new-password"}
          />

          {error && <div className="login__error">{error}</div>}

          <button type="submit" disabled={submitting}>
            {submitting ? "Please wait…" : mode === "login" ? "Sign In." : "Sign Up."}
          </button>
        </form>

        <button
          className="login__toggle"
          onClick={() => {
            setMode(mode === "login" ? "signup" : "login");
            setError("");
          }}
        >
          {mode === "login" ? (
            <>Don't have an account? <strong>Create one</strong></>
          ) : (
            <>Already have an account? <strong>Sign in</strong></>
          )}
        </button>
      </div>
    </div>
  );
}