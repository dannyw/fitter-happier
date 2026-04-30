"""Half Marathons — race analysis dashboard."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DB_PATH = Path("data/fitness.duckdb")

st.set_page_config(page_title="Half Marathons", layout="wide")
st.title("Half Marathons")


@st.cache_resource
def get_con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


con = get_con()

# ---------------------------------------------------------------------------
# Query: race-pace half marathons (20.5-22.5 km, sub-11 min/mi)
# Dedup: when two records share the same date, keep the one with more route
# data (Apple Watch native over Strava import).
# ---------------------------------------------------------------------------
races_df = con.execute("""
    WITH ranked AS (
        SELECT
            w.workout_id,
            w.start_time,
            CAST(w.start_time AS DATE) AS date,
            w.duration_sec,
            w.distance_m,
            w.avg_hr,
            w.max_hr,
            w.elevation_gain_m,
            w.device,
            ROW_NUMBER() OVER (
                PARTITION BY CAST(w.start_time AS DATE)
                ORDER BY (SELECT count(*) FROM routes r WHERE r.workout_id = w.workout_id) DESC
            ) AS rn
        FROM workouts w
        WHERE w.activity_type = 'running'
          AND w.distance_m BETWEEN 20500 AND 22500
          AND w.duration_sec > 0
          AND (w.duration_sec / (w.distance_m / 1609.344) / 60) < 11
    )
    SELECT workout_id, start_time, date, duration_sec, distance_m,
           avg_hr, max_hr, elevation_gain_m, device
    FROM ranked
    WHERE rn = 1
    ORDER BY start_time
""").fetchdf()

if races_df.empty:
    st.info("No half marathon races found.")
    st.stop()

# Derived columns
races_df["date"] = pd.to_datetime(races_df["date"])
miles = races_df["distance_m"] / 1609.344
races_df["miles"] = miles.round(2)
races_df["finish_min"] = (races_df["duration_sec"] / 60).round(1)
races_df["pace_min_mi"] = (races_df["duration_sec"] / miles / 60).round(2)


def _fmt_finish(sec: int) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_pace(sec: float, dist_mi: float) -> str:
    pace_sec = sec / dist_mi
    m, s = divmod(int(pace_sec), 60)
    return f"{m}:{s:02d}"


races_df["finish_fmt"] = races_df["duration_sec"].apply(_fmt_finish)
races_df["pace_fmt"] = [
    _fmt_pace(s, d) for s, d in zip(races_df["duration_sec"], miles, strict=True)
]

# Join weather
weather_df = con.execute(
    "SELECT workout_id, temp_c, humidity_pct, conditions FROM weather"
).fetchdf()
races_df = races_df.merge(weather_df, on="workout_id", how="left")
races_df["temp_f"] = (races_df["temp_c"] * 9 / 5 + 32).round(0)

# --- PR highlight ---
pr_row = races_df.loc[races_df["duration_sec"].idxmin()]
latest = races_df.iloc[-1]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Races", len(races_df))
c2.metric("PR", pr_row["finish_fmt"], help=str(pr_row["date"].date()))
c3.metric("Latest", latest["finish_fmt"], help=str(latest["date"].date()))
delta_sec = latest["duration_sec"] - pr_row["duration_sec"]
c4.metric("vs PR", f"{'+' if delta_sec >= 0 else ''}{delta_sec / 60:+.1f} min")

# --- Race summary table ---
st.subheader("All races")
table_cols = {
    "date": "Date",
    "finish_fmt": "Finish",
    "pace_fmt": "Pace /mi",
    "miles": "Miles",
    "avg_hr": "Avg HR",
    "max_hr": "Max HR",
    "temp_f": "Temp (F)",
    "conditions": "Conditions",
}
display = races_df[list(table_cols.keys())].copy()
display.columns = list(table_cols.values())
display["Date"] = display["Date"].dt.date
st.dataframe(display, use_container_width=True, hide_index=True)

# --- Finish time trend ---
st.subheader("Finish time progression")
fig_finish = go.Figure()
fig_finish.add_trace(
    go.Scatter(
        x=races_df["date"],
        y=races_df["finish_min"],
        mode="lines+markers+text",
        text=races_df["finish_fmt"],
        textposition="top center",
        textfont={"size": 11},
        marker={"size": 10, "color": "royalblue"},
        line={"color": "royalblue", "width": 2},
    )
)
fig_finish.update_layout(
    height=350,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    yaxis_title="Finish (min)",
    showlegend=False,
    hovermode="x unified",
)
st.plotly_chart(fig_finish, use_container_width=True)

# --- Pace vs Avg HR scatter ---
hr_races = races_df.dropna(subset=["avg_hr"])
if not hr_races.empty:
    st.subheader("Pace vs Heart Rate")
    fig_hr = go.Figure()
    fig_hr.add_trace(
        go.Scatter(
            x=hr_races["avg_hr"],
            y=hr_races["pace_min_mi"],
            mode="markers+text",
            text=hr_races["date"].dt.strftime("%Y"),
            textposition="top center",
            marker={"size": 12, "color": "coral"},
        )
    )
    fig_hr.update_layout(
        height=350,
        margin={"l": 40, "r": 20, "t": 20, "b": 30},
        xaxis_title="Avg HR (bpm)",
        yaxis_title="Pace (min/mi)",
        yaxis={"autorange": "reversed"},
        showlegend=False,
    )
    st.plotly_chart(fig_hr, use_container_width=True)

# --- Split-by-split for races with route data ---
st.subheader("Mile splits")

race_options = {
    f"{r['date'].date()} — {r['finish_fmt']}": r["workout_id"]
    for _, r in races_df.iterrows()
}
selected_label = st.selectbox("Select race", list(race_options.keys()), index=len(race_options) - 1)
selected_wid = race_options[selected_label]

# Compute mile splits from route data
route_df = con.execute(
    "SELECT timestamp, lat, lon, elevation_m FROM routes "
    "WHERE workout_id = $1 ORDER BY timestamp",
    [selected_wid],
).fetchdf()

if route_df.empty:
    st.info("No GPS data available for this race.")
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
    cum_mi = cum_m / 1609.344

    route_df["cum_mi"] = cum_mi
    elapsed = (route_df["timestamp"] - route_df["timestamp"].iloc[0]).dt.total_seconds().values

    # Build mile splits
    splits = []
    total_miles = int(cum_mi[-1])
    for mile in range(1, total_miles + 1):
        idx_start = np.searchsorted(cum_mi, mile - 1)
        idx_end = np.searchsorted(cum_mi, mile)
        if idx_end >= len(elapsed):
            idx_end = len(elapsed) - 1
        split_sec = elapsed[idx_end] - elapsed[idx_start]
        sm, ss = divmod(int(split_sec), 60)
        splits.append({"Mile": mile, "Split (sec)": split_sec, "Pace": f"{sm}:{ss:02d}"})

    # Partial last mile
    if cum_mi[-1] > total_miles:
        idx_start = np.searchsorted(cum_mi, total_miles)
        partial_dist = cum_mi[-1] - total_miles
        partial_sec = elapsed[-1] - elapsed[idx_start]
        if partial_dist > 0.05:
            full_pace = partial_sec / partial_dist
            sm, ss = divmod(int(full_pace), 60)
            splits.append({
                "Mile": f"{total_miles + 1} ({partial_dist:.2f})",
                "Split (sec)": partial_sec,
                "Pace": f"{sm}:{ss:02d} (proj)",
            })

    splits_df = pd.DataFrame(splits)

    # Chart + table side by side
    col_chart, col_table = st.columns([2, 1])

    with col_chart:
        numeric_splits = splits_df[splits_df["Mile"].apply(lambda x: isinstance(x, int))].copy()
        avg_pace = numeric_splits["Split (sec)"].mean()
        fig_splits = go.Figure()
        fig_splits.add_trace(
            go.Bar(
                x=numeric_splits["Mile"],
                y=numeric_splits["Split (sec)"],
                text=numeric_splits["Pace"],
                textposition="outside",
                marker_color=[
                    "mediumseagreen" if s <= avg_pace else "salmon"
                    for s in numeric_splits["Split (sec)"]
                ],
            )
        )
        fig_splits.add_hline(
            y=avg_pace,
            line_dash="dash",
            line_color="gray",
            annotation_text=f"avg {int(avg_pace // 60)}:{int(avg_pace % 60):02d}",
        )
        fig_splits.update_layout(
            height=350,
            margin={"l": 40, "r": 20, "t": 20, "b": 30},
            xaxis_title="Mile",
            yaxis_title="Split (sec)",
            showlegend=False,
        )
        st.plotly_chart(fig_splits, use_container_width=True)

    with col_table:
        st.dataframe(splits_df[["Mile", "Pace"]], use_container_width=True, hide_index=True)

# --- HR over time for selected race ---
hr_df = con.execute(
    "SELECT timestamp, value AS hr FROM workout_samples "
    "WHERE workout_id = $1 AND metric = 'heart_rate' ORDER BY timestamp",
    [selected_wid],
).fetchdf()

if len(hr_df) > 10:
    st.subheader("Heart rate — selected race")
    hr_df["timestamp"] = pd.to_datetime(hr_df["timestamp"], utc=True)
    hr_df["elapsed_min"] = (
        (hr_df["timestamp"] - hr_df["timestamp"].iloc[0]).dt.total_seconds() / 60
    )
    fig_hr_trace = go.Figure()
    fig_hr_trace.add_trace(
        go.Scatter(
            x=hr_df["elapsed_min"],
            y=hr_df["hr"],
            mode="lines",
            line={"color": "red", "width": 1.5},
        )
    )
    race_avg = hr_df["hr"].mean()
    fig_hr_trace.add_hline(
        y=race_avg,
        line_dash="dash",
        line_color="gray",
        annotation_text=f"avg {race_avg:.0f}",
    )
    fig_hr_trace.update_layout(
        height=300,
        margin={"l": 40, "r": 20, "t": 20, "b": 30},
        xaxis_title="Elapsed (min)",
        yaxis_title="HR (bpm)",
        showlegend=False,
    )
    st.plotly_chart(fig_hr_trace, use_container_width=True)
