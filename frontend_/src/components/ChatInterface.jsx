import { useEffect, useRef, useState } from "react";
import { askQuestion, getInsights } from "../api";
import ChartDisplay from "./ChartDisplay";
import QueryReceipt from "./QueryReceipt";

function timestamp() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function ChatInterface({ hasData, tables = [] }) {
  const [entries, setEntries] = useState([]);
  const [input, setInput] = useState("");
  const [asking, setAsking] = useState(false);
  const [insightsTable, setInsightsTable] = useState("");
  const [generatingInsights, setGeneratingInsights] = useState(false);
  const feedEndRef = useRef(null);

  useEffect(() => {
    feedEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries, asking, generatingInsights]);

  // Keep the insights table picker pointed at a real table as the list changes
  // (e.g. defaults to the most recently uploaded one).
  useEffect(() => {
    if (tables.length > 0 && !tables.includes(insightsTable)) {
      setInsightsTable(tables[tables.length - 1]);
    }
  }, [tables, insightsTable]);

  async function handleSubmit(e) {
    e.preventDefault();
    const question = input.trim();
    if (!question || asking) return;

    setInput("");
    setAsking(true);
    const questionEntry = { type: "question", text: question, time: timestamp() };
    setEntries((prev) => [...prev, questionEntry]);

    try {
      const result = await askQuestion(question);
      setEntries((prev) => [
        ...prev,
        {
          type: "answer",
          text: result.answer,
          sqlQueries: result.sql_queries,
          chart: result.chart,
          time: timestamp(),
        },
      ]);
    } catch (err) {
      const detail = err?.response?.data?.detail || "Something went wrong answering that.";
      setEntries((prev) => [...prev, { type: "error", text: detail, time: timestamp() }]);
    } finally {
      setAsking(false);
    }
  }

  async function handleGetInsights() {
    if (!insightsTable || generatingInsights) return;

    setGeneratingInsights(true);
    setEntries((prev) => [
      ...prev,
      { type: "question", text: `Generate business insights for ${insightsTable}`, time: timestamp() },
    ]);

    try {
      const result = await getInsights(insightsTable);
      setEntries((prev) => [
        ...prev,
        {
          type: "insights",
          text: result.summary,
          sqlQueries: result.sql_queries,
          charts: result.charts,
          time: timestamp(),
        },
      ]);
    } catch (err) {
      const detail = err?.response?.data?.detail || "Couldn't generate insights for that table.";
      setEntries((prev) => [...prev, { type: "error", text: detail, time: timestamp() }]);
    } finally {
      setGeneratingInsights(false);
    }
  }

  return (
    <div className="chat">
      <div className="chat__feed">
        {entries.length === 0 && (
          <div className="chat__empty">
            {hasData
              ? "Ask a question about your data — for example, \u201Cwhat are total sales by region?\u201D"
              : "Upload a CSV or Excel file above, then ask questions about it in plain English."}
          </div>
        )}

        {entries.map((entry, i) => (
          <div key={i} className={`entry entry--${entry.type === "insights" ? "answer" : entry.type}`}>
            <div className="entry__meta">
              <span className="entry__label">
                {entry.type === "question"
                  ? "You"
                  : entry.type === "error"
                  ? "Error"
                  : entry.type === "insights"
                  ? "Business Insights"
                  : "Analyst"}
              </span>
              <span className="entry__time">{entry.time}</span>
            </div>
            <div className="entry__body">{entry.text}</div>

            {entry.type === "answer" && <ChartDisplay chart={entry.chart} />}
            {entry.type === "insights" &&
              entry.charts?.map((chart, ci) => <ChartDisplay key={ci} chart={chart} />)}

            {(entry.type === "answer" || entry.type === "insights") && (
              <QueryReceipt queries={entry.sqlQueries} />
            )}
          </div>
        ))}

        {asking && (
          <div className="entry entry--answer entry--pending">
            <div className="entry__meta">
              <span className="entry__label">Analyst</span>
            </div>
            <div className="entry__body entry__body--pending">
              Writing a query<span className="dots"><span>.</span><span>.</span><span>.</span></span>
            </div>
          </div>
        )}

        {generatingInsights && (
          <div className="entry entry--answer entry--pending">
            <div className="entry__meta">
              <span className="entry__label">Business Insights</span>
            </div>
            <div className="entry__body entry__body--pending">
              Exploring the data from a few angles<span className="dots"><span>.</span><span>.</span><span>.</span></span>
              <br />
              <span style={{ fontSize: "0.78rem" }}>This runs several queries, so it takes longer than a normal question.</span>
            </div>
          </div>
        )}

        <div ref={feedEndRef} />
      </div>

      {tables.length > 0 && (
        <div className="chat__insights-bar">
          <select
            value={insightsTable}
            onChange={(e) => setInsightsTable(e.target.value)}
            disabled={generatingInsights}
          >
            {tables.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={handleGetInsights}
            disabled={generatingInsights || !insightsTable}
          >
            {generatingInsights ? "Generating…" : "Get Business Insights"}
          </button>
        </div>
      )}

      <form className="chat__input-row" onSubmit={handleSubmit}>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask a question about your data…"
          disabled={asking}
        />
        <button type="submit" disabled={asking || !input.trim()}>
          Ask
        </button>
      </form>
    </div>
  );
}