"""Hiking — hike log and analysis."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DB_PATH = Path("data/fitness.duckdb")

st.set_page_config(page_title="Hiking", layout="wide")
st.title("Hiking")


@st.cache_resource
def get_con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


con = get_con()

# ---------------------------------------------------------------------------
# Query all hikes
# ---------------------------------------------------------------------------
hikes_df = con.execute("""
    SELECT
        workout_id,
        start_time,
        CAST(start_time AS DATE) AS date,
        duration_sec,
        distance_m,
        avg_hr,
        max_hr,
        elevation_gain_m,
        energy_kcal,
        device
    FROM workouts
    WHERE activity_type = 'hiking'
      AND distance_m > 0
      AND duration_sec > 0
    ORDER BY start_time
""").fetchdf()

if hikes_df.empty:
    st.info("No hikes found.")
    st.stop()

# Derived columns
hikes_df["date"] = pd.to_datetime(hikes_df["date"])
hikes_df["miles"] = (hikes_df["distance_m"] / 1609.344).round(2)
hikes_df["duration_hr"] = (hikes_df["duration_sec"] / 3600).round(2)
hikes_df["elev_gain_ft"] = (hikes_df["elevation_gain_m"] * 3.28084).round(0)


def _fmt_duration(sec: int) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_pace(sec: float, dist_mi: float) -> str:
    if dist_mi <= 0:
        return "—"
    pace_sec = sec / dist_mi
    m, s = divmod(int(pace_sec), 60)
    return f"{m}:{s:02d}"


hikes_df["duration_fmt"] = hikes_df["duration_sec"].apply(_fmt_duration)
hikes_df["pace_fmt"] = [
    _fmt_pace(s, d) for s, d in zip(hikes_df["duration_sec"], hikes_df["miles"], strict=True)
]

# Join weather
weather_df = con.execute(
    "SELECT workout_id, temp_c, humidity_pct, conditions FROM weather"
).fetchdf()
hikes_df = hikes_df.merge(weather_df, on="workout_id", how="left")
hikes_df["temp_f"] = (hikes_df["temp_c"] * 9 / 5 + 32).round(0)

# ---------------------------------------------------------------------------
# Metric cards
# ---------------------------------------------------------------------------
total_miles = hikes_df["miles"].sum()
total_elev = hikes_df["elev_gain_ft"].sum()
longest = hikes_df.loc[hikes_df["miles"].idxmax()]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Hikes", len(hikes_df))
c2.metric("Total Miles", f"{total_miles:,.0f}")
c3.metric("Total Elevation", f"{total_elev:,.0f} ft")
c4.metric("Longest Hike", f"{longest['miles']:.1f} mi", help=str(longest["date"].date()))

# ---------------------------------------------------------------------------
# All hikes table
# ---------------------------------------------------------------------------
st.subheader("All hikes")
table_cols = {
    "date": "Date",
    "miles": "Miles",
    "duration_fmt": "Duration",
    "pace_fmt": "Pace /mi",
    "elev_gain_ft": "Elev Gain (ft)",
    "avg_hr": "Avg HR",
    "temp_f": "Temp (F)",
    "conditions": "Conditions",
}
display = hikes_df[list(table_cols.keys())].copy()
display.columns = list(table_cols.values())
display["Date"] = display["Date"].dt.date
st.dataframe(display, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Hikes by year
# ---------------------------------------------------------------------------
st.subheader("Hikes by year")

hikes_df["yr"] = hikes_df["date"].dt.year
yearly = hikes_df.groupby("yr").agg(
    hikes=("workout_id", "count"),
    miles=("miles", "sum"),
    elev_ft=("elev_gain_ft", "sum"),
).reset_index()

fig_yr = go.Figure()
fig_yr.add_trace(
    go.Bar(
        x=yearly["yr"],
        y=yearly["miles"],
        text=yearly["hikes"].apply(lambda n: f"{n} hikes"),
        textposition="outside",
        marker_color="forestgreen",
        hovertemplate="%{x}<br>%{y:.0f} miles<br>%{text}<extra></extra>",
    )
)
fig_yr.update_layout(
    height=350,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    xaxis={"title": "", "dtick": 1},
    yaxis_title="Miles",
    showlegend=False,
)
st.plotly_chart(fig_yr, use_container_width=True)

# ---------------------------------------------------------------------------
# Distance progression over time
# ---------------------------------------------------------------------------
st.subheader("Hike distance over time")
fig_dist = go.Figure()
fig_dist.add_trace(
    go.Scatter(
        x=hikes_df["date"],
        y=hikes_df["miles"],
        mode="markers+lines",
        marker={"size": 8, "color": "forestgreen"},
        line={"color": "forestgreen", "width": 1, "dash": "dot"},
        hovertemplate="%{y:.1f} mi<br>%{x|%b %d, %Y}<extra></extra>",
    )
)
fig_dist.update_layout(
    height=350,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    yaxis_title="Distance (mi)",
    showlegend=False,
    hovermode="x unified",
)
st.plotly_chart(fig_dist, use_container_width=True)

# ---------------------------------------------------------------------------
# Selected hike detail — elevation profile + HR
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Hike detail")

hike_options = {
    f"{r['date'].date()} — {r['miles']:.1f} mi, {r['duration_fmt']}": r["workout_id"]
    for _, r in hikes_df.iterrows()
}
selected_label = st.selectbox("Select hike", list(hike_options.keys()), index=len(hike_options) - 1)
selected_wid = hike_options[selected_label]

# Elevation profile from route data
route_df = con.execute(
    "SELECT timestamp, lat, lon, elevation_m FROM routes "
    "WHERE workout_id = $1 ORDER BY timestamp",
    [selected_wid],
).fetchdf()

if route_df.empty:
    st.info("No GPS data available for this hike.")
else:
    import numpy as np

    route_df["timestamp"] = pd.to_datetime(route_df["timestamp"], utc=True)

    # Haversine cumulative distance
    lat = np.radians(route_df["lat"].values)
    lon = np.radians(route_df["lon"].values)
    dlat = np.diff(lat)
    dlon = np.diff(lon)
    a = np.sin(dlat / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(dlon / 2) ** 2
    c = 2 * np.arcsin(np.sqrt(a))
    seg_m = 6371000 * c
    cum_m = np.concatenate([[0], np.cumsum(seg_m)])
    route_df["cum_mi"] = cum_m / 1609.344

    if route_df["elevation_m"].notna().any():
        st.caption("Elevation profile")
        fig_elev = go.Figure()
        fig_elev.add_trace(
            go.Scatter(
                x=route_df["cum_mi"],
                y=route_df["elevation_m"] * 3.28084,
                mode="lines",
                fill="tozeroy",
                line={"color": "forestgreen", "width": 1.5},
                fillcolor="rgba(34,139,34,0.2)",
            )
        )
        fig_elev.update_layout(
            height=300,
            margin={"l": 40, "r": 20, "t": 20, "b": 30},
            xaxis_title="Distance (mi)",
            yaxis_title="Elevation (ft)",
            showlegend=False,
        )
        st.plotly_chart(fig_elev, use_container_width=True)

# HR over time for selected hike
hr_df = con.execute(
    "SELECT timestamp, value AS hr FROM workout_samples "
    "WHERE workout_id = $1 AND metric = 'heart_rate' ORDER BY timestamp",
    [selected_wid],
).fetchdf()

if len(hr_df) > 10:
    st.caption("Heart rate")
    hr_df["timestamp"] = pd.to_datetime(hr_df["timestamp"], utc=True)
    hr_df["elapsed_min"] = (
        (hr_df["timestamp"] - hr_df["timestamp"].iloc[0]).dt.total_seconds() / 60
    )
    fig_hr = go.Figure()
    fig_hr.add_trace(
        go.Scatter(
            x=hr_df["elapsed_min"],
            y=hr_df["hr"],
            mode="lines",
            line={"color": "red", "width": 1.5},
        )
    )
    hike_avg_hr = hr_df["hr"].mean()
    fig_hr.add_hline(
        y=hike_avg_hr,
        line_dash="dash",
        line_color="gray",
        annotation_text=f"avg {hike_avg_hr:.0f}",
    )
    fig_hr.update_layout(
        height=300,
        margin={"l": 40, "r": 20, "t": 20, "b": 30},
        xaxis_title="Elapsed (min)",
        yaxis_title="HR (bpm)",
        showlegend=False,
    )
    st.plotly_chart(fig_hr, use_container_width=True)
