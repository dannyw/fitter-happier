"""Max Heart Rate — per-workout trends and year-over-year comparison."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DB_PATH = Path("data/fitness.duckdb")

st.set_page_config(page_title="Max Heart Rate", layout="wide")
st.title("Max Heart Rate")


@st.cache_resource
def get_con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


con = get_con()

# --- Query workouts with valid max HR ---
df = con.execute(
    "SELECT start_time::DATE AS date, max_hr, activity_type "
    "FROM workouts "
    "WHERE max_hr IS NOT NULL AND max_hr <= 200 "
    "ORDER BY date",
).fetchdf()

if df.empty:
    st.info("No max heart rate data found.")
    st.stop()

df["date"] = pd.to_datetime(df["date"])

# --- Metric cards ---
recent_180 = df[df["date"] >= df["date"].max() - pd.Timedelta(days=180)]
max_180 = recent_180["max_hr"].max()
max_180_date = recent_180.loc[recent_180["max_hr"].idxmax(), "date"]
recent_90 = df[df["date"] >= df["date"].max() - pd.Timedelta(days=90)]
recent_avg = recent_90["max_hr"].mean()

c1, c2, c3 = st.columns(3)
c1.metric("180-day max", f"{max_180:.0f} bpm", help=f"On {max_180_date:%Y-%m-%d}")
c2.metric("Last 90 days avg", f"{recent_avg:.0f} bpm")
c3.metric("Range", f"{df['max_hr'].min():.0f}–{df['max_hr'].max():.0f} bpm")

# --- Per-workout trend with 30-day rolling average ---
st.subheader("Per-workout trend")
df_sorted = df.sort_values("date")
rolling = df_sorted.set_index("date")["max_hr"].rolling("30D").mean().reset_index()

fig_daily = go.Figure()
fig_daily.add_trace(
    go.Scatter(
        x=df_sorted["date"],
        y=df_sorted["max_hr"],
        mode="markers",
        marker={"size": 3, "color": "rgba(100,149,237,0.3)"},
        name="Workout",
    )
)
fig_daily.add_trace(
    go.Scatter(
        x=rolling["date"],
        y=rolling["max_hr"],
        mode="lines",
        line={"color": "royalblue", "width": 2.5},
        name="30-day avg",
    )
)
fig_daily.update_layout(
    height=350,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    yaxis_title="Max HR (bpm)",
    legend={"orientation": "h", "y": -0.15},
    hovermode="x unified",
)
st.plotly_chart(fig_daily, use_container_width=True)

# --- Max HR by activity type ---
st.subheader("Max HR by activity type")
activity_counts = df.groupby("activity_type")["max_hr"].agg(["mean", "count"]).reset_index()
activity_counts = activity_counts[activity_counts["count"] >= 5].sort_values("mean", ascending=True)

fig_activity = go.Figure()
fig_activity.add_trace(
    go.Bar(
        x=activity_counts["mean"],
        y=activity_counts["activity_type"],
        orientation="h",
        marker_color="cornflowerblue",
    )
)
fig_activity.update_layout(
    height=max(250, len(activity_counts) * 40),
    margin={"l": 120, "r": 20, "t": 20, "b": 30},
    xaxis_title="Avg Max HR (bpm)",
    showlegend=False,
)
event = st.plotly_chart(fig_activity, use_container_width=True, on_select="rerun")

# --- Detail table for selected activity ---
selected_activity = None
if event and event.selection and event.selection.points:
    selected_activity = event.selection.points[0]["y"]

if selected_activity:
    st.markdown(f"**Workouts: {selected_activity}**")
    detail = con.execute(
        "SELECT start_time, activity_type, max_hr, avg_hr, "
        "duration_sec, distance_m, energy_kcal, elevation_gain_m "
        "FROM workouts "
        "WHERE max_hr IS NOT NULL AND max_hr <= 200 AND activity_type = $1 "
        "ORDER BY start_time DESC",
        [selected_activity],
    ).fetchdf()
    detail["date"] = pd.to_datetime(detail["start_time"]).dt.strftime("%Y-%m-%d %H:%M")
    detail["duration"] = (detail["duration_sec"] / 60).round(1).astype(str) + " min"
    detail["distance"] = (detail["distance_m"] / 1609.344).round(2).astype(str) + " mi"
    detail["distance"] = detail["distance"].replace("nan mi", "–")
    cols = [
        "date", "activity_type", "max_hr", "avg_hr",
        "duration", "distance", "energy_kcal", "elevation_gain_m",
    ]
    display = detail[cols].copy()
    display.columns = [
        "Date", "Activity", "Max HR", "Avg HR",
        "Duration", "Distance", "Calories (kcal)", "Elevation (m)",
    ]
    st.dataframe(display, use_container_width=True, hide_index=True)
else:
    st.caption("Click a bar above to see individual workouts for that activity.")

# --- Monthly average max HR ---
st.subheader("Monthly average")
df["year_month"] = df["date"].dt.to_period("M").astype(str)
monthly = df.groupby("year_month")["max_hr"].mean().reset_index()

fig_monthly = go.Figure()
fig_monthly.add_trace(
    go.Bar(
        x=monthly["year_month"],
        y=monthly["max_hr"],
        marker_color="cornflowerblue",
    )
)
fig_monthly.update_layout(
    height=300,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    yaxis_title="Max HR (bpm)",
    xaxis={"tickangle": -45},
    showlegend=False,
)
st.plotly_chart(fig_monthly, use_container_width=True)

# --- Year-over-year overlay ---
st.subheader("Year-over-year comparison")
df["year"] = df["date"].dt.year.astype(str)
df["month"] = df["date"].dt.month
yoy = df.groupby(["year", "month"])["max_hr"].mean().reset_index()

month_labels = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]
colors = {"2023": "gray", "2024": "orange", "2025": "cornflowerblue", "2026": "mediumseagreen"}

fig_yoy = go.Figure()
for year in sorted(yoy["year"].unique()):
    yr = yoy[yoy["year"] == year]
    fig_yoy.add_trace(
        go.Scatter(
            x=[month_labels[m - 1] for m in yr["month"]],
            y=yr["max_hr"],
            mode="lines+markers",
            name=year,
            line={"width": 2.5, "color": colors.get(year, "gray")},
            marker={"size": 6},
        )
    )

fig_yoy.update_layout(
    height=400,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    yaxis_title="Max HR (bpm)",
    xaxis={"categoryorder": "array", "categoryarray": month_labels},
    legend={"orientation": "h", "y": -0.15},
    hovermode="x unified",
)
st.plotly_chart(fig_yoy, use_container_width=True)

# --- Year-over-year delta table ---
st.subheader("Year-over-year delta")
pivot = yoy.pivot(index="month", columns="year", values="max_hr")
pivot.index = [month_labels[m - 1] for m in pivot.index]
years = sorted(pivot.columns)
for i in range(1, len(years)):
    col = f"{years[i]} vs {years[i-1]}"
    pivot[col] = pivot[years[i]] - pivot[years[i - 1]]

display_df = pivot.round(1)
st.dataframe(display_df, use_container_width=True)
