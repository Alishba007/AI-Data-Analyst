import { useState } from "react";

/**
 * The one deliberate signature move in this UI: SQL queries the agent ran
 * are hidden by default and reveal as a monospace "receipt tape" strip
 * with a torn paper edge — ties the visual language back to the ledger/
 * accounting metaphor instead of being a generic "show details" toggle.
 */
export default function QueryReceipt({ queries }) {
  const [open, setOpen] = useState(false);

  if (!queries || queries.length === 0) return null;

  return (
    <div className="receipt">
      <button className="receipt__toggle" onClick={() => setOpen(!open)}>
        {open ? "Hide SQL" : "Show SQL"} <span aria-hidden="true">{open ? "▲" : "▼"}</span>
      </button>
      {open && (
        <div className="receipt__tape">
          <div className="receipt__tear" aria-hidden="true" />
          {queries.map((q, i) => (
            <pre key={i} className="receipt__query">
              {q}
            </pre>
          ))}
        </div>
      )}
    </div>
  );
}
