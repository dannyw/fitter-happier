"""Fitness Dashboard — Home page.

Quick sanity check of the ingested data.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import streamlit as st

DB_PATH = Path("data/fitness.duckdb")

st.set_page_config(page_title="Fitness Dashboard", layout="wide")
st.title("Fitness Dashboard")


@st.cache_resource
def get_con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


con = get_con()

# --- Row counts ---
st.header("Data Summary")
counts = {}
for table in ("workouts", "workout_samples", "health_metrics", "routes", "weather"):
    counts[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Workouts", f"{counts['workouts']:,}")
col2.metric("Samples", f"{counts['workout_samples']:,}")
col3.metric("Health Metrics", f"{counts['health_metrics']:,}")
col4.metric("Route Points", f"{counts['routes']:,}")
col5.metric("Weather", f"{counts['weather']:,}")

# --- Date range ---
st.header("Date Range")
date_range = con.execute(
    "SELECT min(start_time)::DATE, max(start_time)::DATE FROM workouts"
).fetchone()
if date_range[0]:
    st.write(f"**{date_range[0]}** to **{date_range[1]}**")

# --- Activity type breakdown ---
st.header("Workouts by Activity Type")
activity_df = con.execute(
    "SELECT activity_type, count(*) AS n FROM workouts GROUP BY 1 ORDER BY 2 DESC"
).fetchdf()
st.dataframe(activity_df, use_container_width=True, hide_index=True)

# --- Monthly volume ---
st.header("Monthly Workout Count")
monthly_df = con.execute("""
    SELECT strftime(start_time, '%Y-%m') AS month, count(*) AS workouts
    FROM workouts
    GROUP BY 1 ORDER BY 1
""").fetchdf()
st.bar_chart(monthly_df, x="month", y="workouts")

# --- Recent workouts ---
st.header("Recent Workouts (last 20)")
recent_df = con.execute("""
    SELECT
        start_time::DATE AS date,
        activity_type,
        name,
        round(duration_sec / 60.0, 0) AS minutes,
        round(distance_m / 1609.344, 2) AS miles,
        round(energy_kcal, 0) AS kcal,
        avg_hr,
        max_hr,
        device
    FROM workouts
    ORDER BY start_time DESC
    LIMIT 20
""").fetchdf()
st.dataframe(recent_df, use_container_width=True, hide_index=True)

# --- Health metrics breakdown ---
st.header("Health Metrics by Type")
hm_df = con.execute(
    "SELECT metric, count(*) AS n, round(avg(value), 2) AS avg_value, unit "
    "FROM health_metrics GROUP BY metric, unit ORDER BY 2 DESC"
).fetchdf()
st.dataframe(hm_df, use_container_width=True, hide_index=True)
