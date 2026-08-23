"""
chart_generator.py
-------------------
Takes the agent's query results (list of dicts, from agent.AgentResponse.rows)
and automatically picks a sensible Plotly chart — bar, line, scatter, or none
if the data doesn't suit a chart at all.

Returns a plain JSON-safe dict (a Plotly figure spec) that a frontend can
hand directly to react-plotly.js or Plotly.js — no image rendering happens
here, this just decides *what to draw* and produces the spec for it.
"""

import json

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# Matches the frontend's CSS design tokens (App.css) so charts feel like
# part of the same product instead of Plotly's generic default palette.
LEDGER_COLORWAY = ["#2F6F5E", "#C98A2C", "#5B6472", "#B3432B", "#7FA99B", "#E0B368"]

# Below this many rows, a chart usually isn't worth showing — a single
# number or a couple of rows reads better as plain text in the answer.
MIN_ROWS_FOR_CHART = 2

# Above this many rows, bar/scatter charts get unreadable — still chart it,
# but cap what's plotted so labels don't collapse into a solid smear.
MAX_CATEGORIES_PLOTTED = 30


def _is_datetime_like(series: pd.Series) -> bool:
    """
    Rows arrive with dates as ISO strings (agent.py converts them for JSON
    safety), not real datetime objects — so we can't just check dtype.
    Try parsing; if most values convert cleanly, treat it as a time axis.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
        return False
    parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    return parsed.notna().mean() > 0.8  # allow a few stray nulls/garbage


def _classify_columns(df: pd.DataFrame) -> dict:
    """
    Split columns into numeric / datetime-like / categorical, so chart
    selection logic below can reason about shape instead of raw dtypes.
    """
    numeric_cols, datetime_cols, categorical_cols = [], [], []

    for col in df.columns:
        series = df[col]
        if pd.api.types.is_numeric_dtype(series):
            numeric_cols.append(col)
        elif _is_datetime_like(series):
            datetime_cols.append(col)
        else:
            categorical_cols.append(col)

    return {
        "numeric": numeric_cols,
        "datetime": datetime_cols,
        "categorical": categorical_cols,
    }


def _figure_to_json_safe(fig: go.Figure) -> dict:
    """
    Plotly figures can contain numpy int64/float64 values that Python's
    default JSON encoder chokes on. Plotly's own to_json() already handles
    this correctly — round-tripping through it (instead of fig.to_dict())
    guarantees the result is genuinely JSON-safe for FastAPI to return.
    """
    return json.loads(fig.to_json())


def suggest_title(rows: list[dict] | None) -> str | None:
    """
    Guess a readable chart title from column names when no explicit title
    is available (e.g. business insights runs several queries at once —
    there's no single "question" to title each one after). Best-effort:
    "Revenue by Region", "Revenue Over Time", etc. Returns None if the
    data doesn't support a clean guess.
    """
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if df.empty:
        return None

    cols = _classify_columns(df)
    numeric, datetime_cols, categorical = cols["numeric"], cols["datetime"], cols["categorical"]

    def readable(col: str) -> str:
        return col.replace("_", " ").title()

    if datetime_cols and numeric:
        return f"{readable(numeric[0])} Over Time"
    if categorical and numeric:
        return f"{readable(numeric[0])} by {readable(categorical[0])}"
    if len(numeric) >= 2:
        return f"{readable(numeric[0])} vs {readable(numeric[1])}"
    return None


def generate_chart(rows: list[dict] | None, title: str | None = None) -> dict | None:
    """
    Main entry point. Given query result rows, return a Plotly figure spec
    (as a JSON-safe dict) or None if the data doesn't suit a chart.

    Chart selection logic:
      - datetime column + 1 numeric column   -> line chart over time
      - 1 categorical + 1 numeric column     -> bar chart
      - 1 categorical + 2+ numeric columns   -> grouped bar chart
      - 2 numeric columns, no categorical    -> scatter plot
      - anything else (single value, all-text, too few rows) -> None
    """
    if not rows or len(rows) < MIN_ROWS_FOR_CHART:
        return None

    df = pd.DataFrame(rows)
    if df.empty or len(df.columns) < 2:
        return None

    cols = _classify_columns(df)
    numeric, datetime_cols, categorical = cols["numeric"], cols["datetime"], cols["categorical"]

    fig = None

    if datetime_cols and numeric:
        x_col = datetime_cols[0]
        df = df.sort_values(x_col)
        if len(numeric) == 1:
            fig = px.line(df, x=x_col, y=numeric[0], title=title, markers=True,
                           color_discrete_sequence=LEDGER_COLORWAY)
        else:
            fig = px.line(df, x=x_col, y=numeric, title=title, markers=True,
                           color_discrete_sequence=LEDGER_COLORWAY)

    elif categorical and numeric:
        cat_col = categorical[0]
        if len(df) > MAX_CATEGORIES_PLOTTED:
            df = df.nlargest(MAX_CATEGORIES_PLOTTED, numeric[0])
        if len(numeric) == 1:
            fig = px.bar(df, x=cat_col, y=numeric[0], title=title,
                          color_discrete_sequence=LEDGER_COLORWAY)
        else:
            fig = px.bar(df, x=cat_col, y=numeric, title=title, barmode="group",
                          color_discrete_sequence=LEDGER_COLORWAY)

    elif len(numeric) >= 2 and not categorical:
        fig = px.scatter(df, x=numeric[0], y=numeric[1], title=title,
                          color_discrete_sequence=LEDGER_COLORWAY)

    if fig is None:
        return None

    fig.update_layout(
        margin=dict(l=40, r=20, t=50 if title else 20, b=40),
        template="plotly_white",
    )
    return _figure_to_json_safe(fig)


if __name__ == "__main__":
    # Manual smoke tests covering each chart-selection branch.
    print("--- bar chart (categorical + numeric) ---")
    bar_data = [
        {"region": "West", "revenue": 2700.5},
        {"region": "East", "revenue": 1750.75},
        {"region": "South", "revenue": 400.25},
    ]
    result = generate_chart(bar_data, title="Revenue by Region")
    assert result["data"][0]["type"] == "bar", "expected a bar chart"
    print("OK - bar chart, x:", result["data"][0]["x"])

    print("--- line chart (datetime + numeric) ---")
    line_data = [
        {"sale_date": "2024-07-01", "revenue": 1200.5},
        {"sale_date": "2024-07-10", "revenue": 1500.0},
        {"sale_date": "2024-07-20", "revenue": 950.75},
    ]
    result = generate_chart(line_data, title="Revenue Over Time")
    # Plotly Express line charts report type="scatter" with mode="lines+markers"
    # internally — that IS a line chart in Plotly's spec, not a bug.
    assert result["data"][0]["type"] == "scatter", "expected scatter-type trace"
    assert "lines" in result["data"][0]["mode"], "expected a line-mode trace"
    print("OK - line chart, x:", result["data"][0]["x"])

    print("--- scatter (two numeric, no categorical) ---")
    scatter_data = [
        {"price": 19.99, "units_sold": 120},
        {"price": 29.99, "units_sold": 80},
        {"price": 9.99, "units_sold": 300},
    ]
    result = generate_chart(scatter_data, title="Price vs Units Sold")
    assert result["data"][0]["type"] == "scatter", "expected a scatter chart"
    assert result["data"][0].get("mode") in (None, "markers"), "expected marker-only mode, not a line"
    print("OK - scatter chart, x:", result["data"][0]["x"])

    print("--- single value (no chart expected) ---")
    result = generate_chart([{"total_rows": 42}])
    assert result is None, "a single row shouldn't produce a chart"
    print("OK - None as expected")

    print("--- empty (no chart expected) ---")
    result = generate_chart([])
    assert result is None, "empty rows shouldn't produce a chart"
    print("OK - None as expected")

    print("\nAll smoke tests passed.")