"""Resting Heart Rate — month-over-month and year-over-year trends."""

from __future__ import annotations

import datetime
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DB_PATH = Path("data/fitness.duckdb")

st.set_page_config(page_title="Resting Heart Rate", layout="wide")
st.title("Resting Heart Rate")


@st.cache_resource
def get_con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


con = get_con()

# --- Query last 3 years of daily RHR ---
three_years_ago = datetime.date.today() - datetime.timedelta(days=3 * 365)

df = con.execute(
    "SELECT start_time::DATE AS date, avg(value) AS rhr "
    "FROM health_metrics "
    "WHERE metric = $1 AND start_time >= $2 "
    "GROUP BY 1 ORDER BY 1",
    ["resting_heart_rate", str(three_years_ago)],
).fetchdf()

if df.empty:
    st.info("No resting heart rate data found.")
    st.stop()

df["date"] = pd.to_datetime(df["date"])

# --- Metric cards: current, 3-year avg, 3-year range ---
recent_avg = df[df["date"] >= df["date"].max() - pd.Timedelta(days=7)]["rhr"].mean()
overall_avg = df["rhr"].mean()
c1, c2, c3 = st.columns(3)
c1.metric("Last 7 days", f"{recent_avg:.0f} bpm")
c2.metric("3-year avg", f"{overall_avg:.0f} bpm")
c3.metric("Range", f"{df['rhr'].min():.0f}–{df['rhr'].max():.0f} bpm")

# --- Daily trend with 30-day rolling average ---
st.subheader("Daily trend")
df_sorted = df.sort_values("date")
rolling = df_sorted.set_index("date")["rhr"].rolling("30D").mean().reset_index()

fig_daily = go.Figure()
fig_daily.add_trace(
    go.Scatter(
        x=df_sorted["date"],
        y=df_sorted["rhr"],
        mode="markers",
        marker={"size": 3, "color": "rgba(100,149,237,0.3)"},
        name="Daily",
    )
)
fig_daily.add_trace(
    go.Scatter(
        x=rolling["date"],
        y=rolling["rhr"],
        mode="lines",
        line={"color": "royalblue", "width": 2.5},
        name="30-day avg",
    )
)
fig_daily.update_layout(
    height=350,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    yaxis_title="RHR (bpm)",
    legend={"orientation": "h", "y": -0.15},
    hovermode="x unified",
)
st.plotly_chart(fig_daily, use_container_width=True)

# --- Month-over-month bar chart ---
st.subheader("Monthly average")
df["year_month"] = df["date"].dt.to_period("M").astype(str)
monthly = df.groupby("year_month")["rhr"].mean().reset_index()

fig_monthly = go.Figure()
fig_monthly.add_trace(
    go.Bar(
        x=monthly["year_month"],
        y=monthly["rhr"],
        marker_color="cornflowerblue",
    )
)
fig_monthly.update_layout(
    height=300,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    yaxis_title="RHR (bpm)",
    xaxis={"tickangle": -45},
    showlegend=False,
)
st.plotly_chart(fig_monthly, use_container_width=True)

# --- Year-over-year overlay ---
st.subheader("Year-over-year comparison")
df["year"] = df["date"].dt.year.astype(str)
df["month"] = df["date"].dt.month
yoy = df.groupby(["year", "month"])["rhr"].mean().reset_index()

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
            y=yr["rhr"],
            mode="lines+markers",
            name=year,
            line={"width": 2.5, "color": colors.get(year, "gray")},
            marker={"size": 6},
        )
    )

fig_yoy.update_layout(
    height=400,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    yaxis_title="RHR (bpm)",
    xaxis={"categoryorder": "array", "categoryarray": month_labels},
    legend={"orientation": "h", "y": -0.15},
    hovermode="x unified",
)
st.plotly_chart(fig_yoy, use_container_width=True)

# --- Year-over-year delta table ---
st.subheader("Year-over-year delta")
pivot = yoy.pivot(index="month", columns="year", values="rhr")
pivot.index = [month_labels[m - 1] for m in pivot.index]
years = sorted(pivot.columns)
for i in range(1, len(years)):
    col = f"{years[i]} vs {years[i-1]}"
    pivot[col] = pivot[years[i]] - pivot[years[i - 1]]

display_df = pivot.round(1)
st.dataframe(display_df, use_container_width=True)
