import Plot from "react-plotly.js";

/**
 * Renders a Plotly figure spec exactly as returned by chart_generator.py's
 * generate_chart() — the backend decides bar/line/scatter, this component
 * just draws whatever JSON it gets. If chart is null (backend decided the
 * data didn't suit a chart), this renders nothing.
 */
export default function ChartDisplay({ chart }) {
  if (!chart) return null;

  return (
    <div className="chart-display">
      <Plot
        data={chart.data}
        layout={{
          ...chart.layout,
          autosize: true,
          font: { family: "IBM Plex Sans, sans-serif", size: 12, color: "#1B2430" },
          paper_bgcolor: "transparent",
          plot_bgcolor: "transparent",
        }}
        config={{ displayModeBar: false, responsive: true }}
        style={{ width: "100%", height: "320px" }}
        useResizeHandler
      />
    </div>
  );
}
