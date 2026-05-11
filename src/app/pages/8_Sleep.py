"""Sleep — nightly trends, stage breakdown, and correlated factors."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DB_PATH = Path("data/fitness.duckdb")

st.set_page_config(page_title="Sleep", layout="wide")
st.title("Sleep")


@st.cache_resource
def get_con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


con = get_con()

# ---------------------------------------------------------------------------
# Core data query — aggregate sleep segments into nightly summaries
# ---------------------------------------------------------------------------
df = con.execute("""
    WITH nightly AS (
        SELECT
            CASE WHEN EXTRACT(HOUR FROM start_time) >= 20
                 THEN CAST(start_time AS DATE)
                 ELSE CAST(start_time AS DATE) - INTERVAL 1 DAY
            END AS sleep_date,
            metric,
            SUM(value) AS hours
        FROM health_metrics
        WHERE metric IN ('sleep_core', 'sleep_deep', 'sleep_rem',
                         'sleep_awake', 'sleep_in_bed')
        GROUP BY 1, 2
    )
    SELECT
        CAST(sleep_date AS DATE) AS sleep_date,
        COALESCE(SUM(hours) FILTER (WHERE metric = 'sleep_core'), 0) AS core,
        COALESCE(SUM(hours) FILTER (WHERE metric = 'sleep_deep'), 0) AS deep,
        COALESCE(SUM(hours) FILTER (WHERE metric = 'sleep_rem'), 0) AS rem,
        COALESCE(SUM(hours) FILTER (WHERE metric = 'sleep_awake'), 0) AS awake,
        COALESCE(SUM(hours) FILTER (WHERE metric = 'sleep_in_bed'), 0) AS in_bed
    FROM nightly
    GROUP BY 1
    ORDER BY 1
""").fetchdf()

if df.empty:
    st.info("No sleep data found.")
    st.stop()

df["sleep_date"] = pd.to_datetime(df["sleep_date"])
df["actual_sleep"] = df["core"] + df["deep"] + df["rem"]

# Efficiency: use in_bed if it covers actual sleep, else actual/(actual+awake)
def _efficiency(r: pd.Series) -> float | None:
    if r["in_bed"] >= r["actual_sleep"] and r["in_bed"] > 0:
        return r["actual_sleep"] / r["in_bed"]
    denom = r["actual_sleep"] + r["awake"]
    if denom > 0:
        return min(r["actual_sleep"] / denom, 1.0)
    return None


df["efficiency"] = df.apply(_efficiency, axis=1)

# Filter implausible nights
df = df[(df["actual_sleep"] >= 1) & (df["actual_sleep"] <= 14)].copy()

if df.empty:
    st.info("No plausible sleep nights found.")
    st.stop()


def _fmt_hours(h: float) -> str:
    hours = int(h)
    minutes = int((h - hours) * 60)
    return f"{hours}h {minutes}m"


# ---------------------------------------------------------------------------
# Metric cards
# ---------------------------------------------------------------------------
best_eff_idx = df["efficiency"].idxmax()
best_eff_row = df.loc[best_eff_idx]

c1, c2, c3 = st.columns(3)
c1.metric("Nights tracked", f"{len(df):,}")
c2.metric("Average sleep", _fmt_hours(df["actual_sleep"].mean()))
c3.metric(
    "Best efficiency",
    f"{best_eff_row['efficiency']:.0%}",
    help=f"On {best_eff_row['sleep_date']:%Y-%m-%d}",
)

# ---------------------------------------------------------------------------
# Nightly sleep trend
# ---------------------------------------------------------------------------
st.subheader("Nightly sleep trend")
df_sorted = df.sort_values("sleep_date")
roll_7 = df_sorted.set_index("sleep_date")["actual_sleep"].rolling("7D").mean().reset_index()
roll_30 = df_sorted.set_index("sleep_date")["actual_sleep"].rolling("30D").mean().reset_index()

fig_trend = go.Figure()
fig_trend.add_trace(
    go.Scatter(
        x=df_sorted["sleep_date"],
        y=df_sorted["actual_sleep"],
        mode="markers",
        marker={"size": 3, "color": "rgba(138,92,198,0.3)"},
        name="Nightly",
    )
)
fig_trend.add_trace(
    go.Scatter(
        x=roll_7["sleep_date"],
        y=roll_7["actual_sleep"],
        mode="lines",
        line={"color": "mediumpurple", "width": 2},
        name="7-day avg",
    )
)
fig_trend.add_trace(
    go.Scatter(
        x=roll_30["sleep_date"],
        y=roll_30["actual_sleep"],
        mode="lines",
        line={"color": "indigo", "width": 2.5},
        name="30-day avg",
    )
)
fig_trend.update_layout(
    height=350,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    yaxis_title="Sleep (hours)",
    legend={"orientation": "h", "y": -0.15},
    hovermode="x unified",
)
st.plotly_chart(fig_trend, use_container_width=True)

# ---------------------------------------------------------------------------
# Sleep stages stacked area
# ---------------------------------------------------------------------------
st.subheader("Sleep stages over time")
fig_stages = go.Figure()
for col, color, label in [
    ("core", "slateblue", "Core"),
    ("deep", "darkslateblue", "Deep"),
    ("rem", "mediumpurple", "REM"),
    ("awake", "orange", "Awake"),
]:
    fig_stages.add_trace(
        go.Scatter(
            x=df_sorted["sleep_date"],
            y=df_sorted[col],
            mode="lines",
            name=label,
            line={"color": color, "width": 0.5},
            stackgroup="one",
        )
    )
fig_stages.update_layout(
    height=350,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    yaxis_title="Hours",
    legend={"orientation": "h", "y": -0.15},
    hovermode="x unified",
)
st.plotly_chart(fig_stages, use_container_width=True)

# ---------------------------------------------------------------------------
# Day-of-week patterns
# ---------------------------------------------------------------------------
st.subheader("Average sleep by day of week")
df["dow"] = df["sleep_date"].dt.dayofweek  # 0=Mon
dow_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
dow_avg = df.groupby("dow")["actual_sleep"].mean().reindex(range(7))
overall_avg = df["actual_sleep"].mean()

fig_dow = go.Figure()
fig_dow.add_trace(
    go.Bar(
        x=dow_labels,
        y=dow_avg.values,
        marker_color="mediumpurple",
    )
)
fig_dow.add_hline(
    y=overall_avg,
    line_dash="dash",
    line_color="gray",
    annotation_text=f"avg {_fmt_hours(overall_avg)}",
)
fig_dow.update_layout(
    height=300,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    yaxis_title="Hours",
    xaxis={"categoryorder": "array", "categoryarray": dow_labels},
    showlegend=False,
)
st.plotly_chart(fig_dow, use_container_width=True)

# ---------------------------------------------------------------------------
# Best 10 nights (last 24 months)
# ---------------------------------------------------------------------------
st.subheader("Best 10 nights (last 24 months)")
cutoff = df["sleep_date"].max() - pd.Timedelta(days=730)
recent = df[df["sleep_date"] >= cutoff].copy()
best10 = recent.nlargest(10, "efficiency")

# Summary table
best_display = best10[["sleep_date", "actual_sleep", "efficiency", "deep", "rem", "core"]].copy()
best_display["sleep_date"] = best_display["sleep_date"].dt.date
best_display["actual_sleep"] = best_display["actual_sleep"].round(2)
best_display["efficiency"] = (best_display["efficiency"] * 100).round(1)
best_display["deep"] = best_display["deep"].round(2)
best_display["rem"] = best_display["rem"].round(2)
best_display["core"] = best_display["core"].round(2)
best_display.columns = [
    "Date", "Total Sleep (h)", "Efficiency %", "Deep (h)", "REM (h)", "Core (h)",
]
st.dataframe(best_display, use_container_width=True, hide_index=True)

# Horizontal bar chart with click interaction
best10_sorted = best10.sort_values("efficiency", ascending=True)
best10_sorted["label"] = best10_sorted["sleep_date"].dt.strftime("%Y-%m-%d")

fig_best = go.Figure()
fig_best.add_trace(
    go.Bar(
        x=best10_sorted["efficiency"],
        y=best10_sorted["label"],
        orientation="h",
        marker_color="mediumpurple",
        hovertemplate="%{y}<br>Efficiency: %{x:.1%}<extra></extra>",
    )
)
fig_best.update_layout(
    height=max(250, len(best10_sorted) * 35),
    margin={"l": 100, "r": 20, "t": 20, "b": 30},
    xaxis_title="Sleep Efficiency",
    xaxis={"tickformat": ".0%"},
    showlegend=False,
)
event = st.plotly_chart(fig_best, use_container_width=True, on_select="rerun")

# ---------------------------------------------------------------------------
# Correlated factors (conditional on bar click)
# ---------------------------------------------------------------------------
selected_date = None
if event and event.selection and event.selection.points:
    selected_label = event.selection.points[0]["y"]
    selected_date = pd.to_datetime(selected_label).date()

if selected_date:
    st.divider()
    st.subheader(f"Correlated factors — {selected_date}")

    sel_row = best10[best10["sleep_date"].dt.date == selected_date].iloc[0]

    # --- Sleep stage breakdown ---
    st.caption("Sleep stage breakdown")
    stages = {
        "Core": sel_row["core"],
        "Deep": sel_row["deep"],
        "REM": sel_row["rem"],
        "Awake": sel_row["awake"],
    }
    stage_colors = {
        "Core": "slateblue",
        "Deep": "darkslateblue",
        "REM": "mediumpurple",
        "Awake": "orange",
    }
    fig_breakdown = go.Figure()
    fig_breakdown.add_trace(
        go.Bar(
            x=list(stages.values()),
            y=list(stages.keys()),
            orientation="h",
            marker_color=[stage_colors[k] for k in stages],
            hovertemplate="%{y}: %{x:.2f}h<extra></extra>",
        )
    )
    fig_breakdown.update_layout(
        height=200,
        margin={"l": 60, "r": 20, "t": 10, "b": 30},
        xaxis_title="Hours",
        showlegend=False,
    )
    st.plotly_chart(fig_breakdown, use_container_width=True)

    # --- RHR + HRV ---
    st.caption("Resting Heart Rate & HRV")
    vitals = con.execute(
        "SELECT metric, AVG(value) AS val "
        "FROM health_metrics "
        "WHERE CAST(start_time AS DATE) = $1 "
        "  AND metric IN ('resting_heart_rate', 'heart_rate_variability') "
        "GROUP BY metric",
        [str(selected_date)],
    ).fetchdf()
    if vitals.empty:
        st.info("No RHR/HRV data for this date.")
    else:
        rhr_val = vitals.loc[vitals["metric"] == "resting_heart_rate", "val"]
        hrv_val = vitals.loc[vitals["metric"] == "heart_rate_variability", "val"]
        vc1, vc2 = st.columns(2)
        if not rhr_val.empty:
            vc1.metric("Resting HR", f"{rhr_val.iloc[0]:.0f} bpm")
        else:
            vc1.info("No RHR data")
        if not hrv_val.empty:
            vc2.metric("HRV", f"{hrv_val.iloc[0]:.0f} ms")
        else:
            vc2.info("No HRV data")

    # --- Training load ---
    st.caption("Training load")
    tl = con.execute(
        "SELECT ctl, atl, tsb FROM training_load WHERE date = $1",
        [str(selected_date)],
    ).fetchdf()
    if tl.empty:
        st.info("No training load data for this date.")
    else:
        tc1, tc2, tc3 = st.columns(3)
        tc1.metric("CTL (fitness)", f"{tl['ctl'].iloc[0]:.1f}")
        tc2.metric("ATL (fatigue)", f"{tl['atl'].iloc[0]:.1f}")
        tc3.metric("TSB (form)", f"{tl['tsb'].iloc[0]:.1f}")

    # --- Workouts that day ---
    st.caption("Workouts that day")
    workouts = con.execute(
        "SELECT activity_type, name, duration_sec, distance_m, energy_kcal "
        "FROM workouts "
        "WHERE CAST(start_time AS DATE) = $1 "
        "ORDER BY start_time",
        [str(selected_date)],
    ).fetchdf()
    if workouts.empty:
        st.info("No workouts on this date.")
    else:
        workouts["duration"] = (workouts["duration_sec"] / 60).round(1)
        workouts["distance"] = (workouts["distance_m"] / 1609.344).round(2)
        cols = ["activity_type", "name", "duration", "distance", "energy_kcal"]
        w_display = workouts[cols].copy()
        w_display.columns = ["Activity", "Name", "Duration (min)", "Distance (mi)", "Calories"]
        st.dataframe(w_display, use_container_width=True, hide_index=True)
else:
    st.caption("Click a bar above to see correlated factors for that night.")
