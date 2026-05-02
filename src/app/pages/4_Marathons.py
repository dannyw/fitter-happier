"""Marathons — race analysis dashboard."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DB_PATH = Path("data/fitness.duckdb")

st.set_page_config(page_title="Marathons", layout="wide")
st.title("Marathons")


@st.cache_resource
def get_con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


con = get_con()

# ---------------------------------------------------------------------------
# Query: marathon-distance runs (25.5-27.5 miles, sub-14 min/mi pace)
# Dedup: when two records share the same date, keep the one with more route data.
# ---------------------------------------------------------------------------
races_df = con.execute("""
    WITH ranked AS (
        SELECT
            w.workout_id,
            w.name,
            w.start_time,
            CAST(w.start_time AS DATE) AS date,
            w.duration_sec,
            w.distance_m,
            w.avg_hr,
            w.max_hr,
            w.elevation_gain_m,
            w.device,
            w.raw,
            ROW_NUMBER() OVER (
                PARTITION BY CAST(w.start_time AS DATE)
                ORDER BY (SELECT count(*) FROM routes r WHERE r.workout_id = w.workout_id) DESC
            ) AS rn
        FROM workouts w
        WHERE w.activity_type = 'running'
          AND w.distance_m / 1609.344 BETWEEN 25.5 AND 27.5
          AND w.duration_sec > 0
          AND (w.duration_sec / (w.distance_m / 1609.344) / 60) < 14
    )
    SELECT workout_id, name, start_time, date, duration_sec, distance_m,
           avg_hr, max_hr, elevation_gain_m, device, raw
    FROM ranked
    WHERE rn = 1
    ORDER BY start_time
""").fetchdf()

if races_df.empty:
    st.info("No marathon races found.")
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
    return f"{h}:{m:02d}:{s:02d}"


def _fmt_pace(sec: float, dist_mi: float) -> str:
    pace_sec = sec / dist_mi
    m, s = divmod(int(pace_sec), 60)
    return f"{m}:{s:02d}"


races_df["finish_fmt"] = races_df["duration_sec"].apply(_fmt_finish)
races_df["pace_fmt"] = [
    _fmt_pace(s, d) for s, d in zip(races_df["duration_sec"], miles, strict=True)
]

# Extract description/gear from raw JSON (Strava metadata)
def _extract_description(raw_str: str) -> str:
    try:
        data = json.loads(raw_str)
        return (
            data.get("strava_description")
            or data.get("Activity Description")
            or ""
        )
    except (json.JSONDecodeError, TypeError):
        return ""


def _extract_gear(raw_str: str) -> str:
    try:
        data = json.loads(raw_str)
        return (
            data.get("strava_gear")
            or data.get("Activity Gear")
            or ""
        )
    except (json.JSONDecodeError, TypeError):
        return ""


races_df["description"] = races_df["raw"].apply(_extract_description)
races_df["gear"] = races_df["raw"].apply(_extract_gear)

# Join weather
weather_df = con.execute(
    "SELECT workout_id, temp_c, humidity_pct, wind_speed_mps, conditions FROM weather"
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
sign = "+" if delta_sec >= 0 else ""
c4.metric("vs PR", f"{sign}{delta_sec / 60:.1f} min")

# --- Race summary table ---
st.subheader("All races")
table_data = races_df[
    ["date", "name", "finish_fmt", "pace_fmt", "miles", "avg_hr", "max_hr",
     "elevation_gain_m", "temp_f", "conditions", "gear"]
].copy()
table_data.columns = [
    "Date", "Name", "Finish", "Pace /mi", "Miles", "Avg HR", "Max HR",
    "Elevation (m)", "Temp (F)", "Conditions", "Gear",
]
table_data["Date"] = table_data["Date"].dt.date
st.dataframe(table_data, use_container_width=True, hide_index=True)

# Show description if any race has one
descs = races_df[races_df["description"].str.len() > 0]
if not descs.empty:
    for _, r in descs.iterrows():
        st.caption(f"**{r['date'].date()} — {r['name']}**: {r['description']}")

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
        hovertemplate="%{text}<br>%{customdata}<extra></extra>",
        customdata=races_df["name"],
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

# --- Mile splits for selected race ---
st.subheader("Mile splits")

def _race_label(r: pd.Series) -> str:
    base = f"{r['date'].date()} — {r['finish_fmt']}"
    if pd.notna(r.get("name")) and r["name"]:
        return f"{r['name']} ({base})"
    return base


race_options = {
    _race_label(r): r["workout_id"]
    for _, r in races_df.iterrows()
}
selected_label = st.selectbox("Select race", list(race_options.keys()), index=len(race_options) - 1)
selected_wid = race_options[selected_label]

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

    # First/second half split analysis
    numeric_splits = splits_df[splits_df["Mile"].apply(lambda x: isinstance(x, int))].copy()
    if len(numeric_splits) >= 20:
        half = len(numeric_splits) // 2
        first_half = numeric_splits.iloc[:half]["Split (sec)"].sum()
        second_half = numeric_splits.iloc[half:]["Split (sec)"].sum()
        diff = second_half - first_half
        h1m, h1s = divmod(int(first_half), 60)
        h2m, h2s = divmod(int(second_half), 60)
        dm, ds = divmod(abs(int(diff)), 60)
        split_type = "positive" if diff > 0 else "negative" if diff < 0 else "even"

        sc1, sc2, sc3 = st.columns(3)
        sc1.metric("First half", f"{h1m}:{h1s:02d}")
        sc2.metric("Second half", f"{h2m}:{h2s:02d}")
        sc3.metric("Split", f"{'+'if diff > 0 else '-'}{dm}:{ds:02d} ({split_type})")

    # Chart + table side by side
    col_chart, col_table = st.columns([2, 1])

    with col_chart:
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
            height=400,
            margin={"l": 40, "r": 20, "t": 20, "b": 30},
            xaxis_title="Mile",
            yaxis_title="Split (sec)",
            showlegend=False,
        )
        st.plotly_chart(fig_splits, use_container_width=True)

    with col_table:
        st.dataframe(splits_df[["Mile", "Pace"]], use_container_width=True, hide_index=True,
                      height=400)

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

# --- Elevation profile for selected race ---
if not route_df.empty and route_df["elevation_m"].notna().any():
    st.subheader("Elevation profile — selected race")
    fig_elev = go.Figure()
    fig_elev.add_trace(
        go.Scatter(
            x=route_df["cum_mi"],
            y=route_df["elevation_m"] * 3.28084,  # convert to feet
            mode="lines",
            fill="tozeroy",
            line={"color": "forestgreen", "width": 1.5},
            fillcolor="rgba(34,139,34,0.2)",
        )
    )
    fig_elev.update_layout(
        height=250,
        margin={"l": 40, "r": 20, "t": 20, "b": 30},
        xaxis_title="Distance (mi)",
        yaxis_title="Elevation (ft)",
        showlegend=False,
    )
    st.plotly_chart(fig_elev, use_container_width=True)

# ===========================================================================
# Training Preparation — 16-week build-up for the selected race
# ===========================================================================
st.divider()
st.header("Training preparation — 16-week build-up")

race_row = races_df[races_df["workout_id"] == selected_wid].iloc[0]
race_date = pd.to_datetime(race_row["start_time"], utc=True)
training_start = race_date - pd.Timedelta(weeks=16)


def _weeks_to_race(week: pd.Timestamp, race: pd.Timestamp) -> int:
    return int((week - race).days // 7)


# --- Panel 1: Weekly Running Mileage (long run highlighted) ----------------
st.subheader("Weekly running mileage")

mileage_df = con.execute(
    """
    SELECT
        DATE_TRUNC('week', start_time) AS week,
        SUM(distance_m) / 1609.344 AS total_miles,
        MAX(distance_m) / 1609.344 AS longest_run_miles,
        COUNT(*) AS runs
    FROM workouts
    WHERE activity_type = 'running'
      AND distance_m > 0
      AND start_time >= $1
      AND start_time < $2
    GROUP BY DATE_TRUNC('week', start_time)
    ORDER BY week
    """,
    [training_start, race_date],
).fetchdf()

if mileage_df.empty:
    st.info("No running data in the 16-week window.")
else:
    mileage_df["week"] = pd.to_datetime(mileage_df["week"], utc=True)
    mileage_df["wk_label"] = mileage_df["week"].apply(
        lambda w: _weeks_to_race(w, race_date)
    )
    mileage_df["other_miles"] = mileage_df["total_miles"] - mileage_df["longest_run_miles"]

    fig_mi = go.Figure()
    fig_mi.add_trace(
        go.Bar(
            x=mileage_df["wk_label"],
            y=mileage_df["other_miles"],
            name="Other runs",
            marker_color="lightsteelblue",
        )
    )
    fig_mi.add_trace(
        go.Bar(
            x=mileage_df["wk_label"],
            y=mileage_df["longest_run_miles"],
            name="Long run",
            marker_color="steelblue",
        )
    )
    fig_mi.update_layout(
        barmode="stack",
        height=350,
        margin={"l": 40, "r": 20, "t": 20, "b": 30},
        xaxis_title="Weeks to race",
        yaxis_title="Miles",
        legend={"orientation": "h", "y": -0.2},
        hovermode="x unified",
    )
    st.plotly_chart(fig_mi, use_container_width=True)

# --- Panel 2: Resting Heart Rate Weekly Trend ------------------------------
st.subheader("Resting heart rate trend")

rhr_df = con.execute(
    """
    SELECT
        DATE_TRUNC('week', start_time) AS week,
        AVG(value) AS avg_rhr,
        MIN(value) AS min_rhr,
        MAX(value) AS max_rhr
    FROM health_metrics
    WHERE metric = 'resting_heart_rate'
      AND start_time >= $1
      AND start_time < $2
    GROUP BY DATE_TRUNC('week', start_time)
    ORDER BY week
    """,
    [training_start, race_date],
).fetchdf()

if rhr_df.empty:
    st.info("No resting heart rate data available for this training window (data starts ~2018).")
else:
    rhr_df["week"] = pd.to_datetime(rhr_df["week"], utc=True)
    rhr_df["wk_label"] = rhr_df["week"].apply(lambda w: _weeks_to_race(w, race_date))

    fig_rhr = go.Figure()
    # Min/max band
    fig_rhr.add_trace(
        go.Scatter(
            x=rhr_df["wk_label"],
            y=rhr_df["max_rhr"],
            mode="lines",
            line={"width": 0},
            showlegend=False,
        )
    )
    fig_rhr.add_trace(
        go.Scatter(
            x=rhr_df["wk_label"],
            y=rhr_df["min_rhr"],
            mode="lines",
            line={"width": 0},
            fill="tonexty",
            fillcolor="rgba(255,0,0,0.1)",
            showlegend=False,
        )
    )
    # Avg line
    fig_rhr.add_trace(
        go.Scatter(
            x=rhr_df["wk_label"],
            y=rhr_df["avg_rhr"],
            mode="lines+markers",
            line={"color": "red", "width": 2},
            marker={"size": 5},
            name="Avg RHR",
        )
    )
    fig_rhr.update_layout(
        height=300,
        margin={"l": 40, "r": 20, "t": 20, "b": 30},
        xaxis_title="Weeks to race",
        yaxis_title="RHR (bpm)",
        showlegend=False,
        hovermode="x unified",
    )
    st.plotly_chart(fig_rhr, use_container_width=True)

# --- Panel 3: Fitness / Fatigue / Form (CTL/ATL/TSB) ----------------------
st.subheader("Fitness / Fatigue / Form")

tl_df = con.execute(
    """
    SELECT date, ctl, atl, tsb
    FROM training_load
    WHERE date >= $1 AND date <= $2
    ORDER BY date
    """,
    [training_start.date(), race_date.date()],
).fetchdf()

if tl_df.empty:
    st.info("No training load data available for this window (data starts ~2022).")
else:
    tl_df["date"] = pd.to_datetime(tl_df["date"])

    # Metric cards for race-day values
    race_day_tl = tl_df.iloc[-1]
    tc1, tc2, tc3 = st.columns(3)
    tc1.metric("Fitness (CTL)", f"{race_day_tl['ctl']:.1f}")
    tc2.metric("Fatigue (ATL)", f"{race_day_tl['atl']:.1f}")
    tc3.metric("Form (TSB)", f"{race_day_tl['tsb']:.1f}")

    fig_tl = go.Figure()
    # TSB filled area
    fig_tl.add_trace(
        go.Scatter(
            x=tl_df["date"], y=tl_df["tsb"].clip(lower=0),
            fill="tozeroy", fillcolor="rgba(0,200,0,0.15)",
            line={"color": "rgba(0,200,0,0.4)", "width": 0},
            showlegend=False,
        )
    )
    fig_tl.add_trace(
        go.Scatter(
            x=tl_df["date"], y=tl_df["tsb"].clip(upper=0),
            fill="tozeroy", fillcolor="rgba(200,0,0,0.15)",
            line={"color": "rgba(200,0,0,0.4)", "width": 0},
            showlegend=False,
        )
    )
    # TSB line
    fig_tl.add_trace(
        go.Scatter(
            x=tl_df["date"], y=tl_df["tsb"],
            mode="lines", line={"color": "gray", "width": 1, "dash": "dot"},
            name="Form (TSB)",
        )
    )
    # CTL
    fig_tl.add_trace(
        go.Scatter(
            x=tl_df["date"], y=tl_df["ctl"],
            mode="lines", line={"color": "royalblue", "width": 2},
            name="Fitness (CTL)",
        )
    )
    # ATL
    fig_tl.add_trace(
        go.Scatter(
            x=tl_df["date"], y=tl_df["atl"],
            mode="lines", line={"color": "orange", "width": 2},
            name="Fatigue (ATL)",
        )
    )
    fig_tl.update_layout(
        height=400,
        margin={"l": 40, "r": 20, "t": 20, "b": 30},
        xaxis_title="Date",
        yaxis_title="CTL / ATL / TSB",
        legend={"orientation": "h", "y": -0.15},
        hovermode="x unified",
    )
    st.plotly_chart(fig_tl, use_container_width=True)

# --- Panel 4: Long Run Progression ----------------------------------------
st.subheader("Long run progression")

long_runs_df = con.execute(
    """
    SELECT start_time, distance_m / 1609.344 AS miles, duration_sec
    FROM workouts
    WHERE activity_type = 'running'
      AND distance_m / 1609.344 >= 10
      AND start_time >= $1
      AND start_time < $2
    ORDER BY start_time
    """,
    [training_start, race_date],
).fetchdf()

if long_runs_df.empty:
    st.info("No long runs (≥ 10 mi) in this training window.")
else:
    long_runs_df["start_time"] = pd.to_datetime(long_runs_df["start_time"], utc=True)
    long_runs_df["pace_min_mi"] = (long_runs_df["duration_sec"] / long_runs_df["miles"] / 60)

    fig_long = go.Figure()
    fig_long.add_trace(
        go.Scatter(
            x=long_runs_df["start_time"],
            y=long_runs_df["miles"],
            mode="markers+lines",
            marker={"size": 10, "color": "steelblue"},
            line={"color": "steelblue", "width": 1, "dash": "dot"},
            hovertemplate="%.1f mi<br>%{x|%b %d}<extra></extra>",
        )
    )
    for ref in [16, 18, 20]:
        fig_long.add_hline(
            y=ref, line_dash="dash", line_color="lightgray",
            annotation_text=f"{ref} mi", annotation_position="bottom right",
        )
    fig_long.update_layout(
        height=350,
        margin={"l": 40, "r": 20, "t": 20, "b": 30},
        xaxis_title="Date",
        yaxis_title="Distance (mi)",
        showlegend=False,
        hovermode="x unified",
    )
    st.plotly_chart(fig_long, use_container_width=True)

# --- Panel 5: Weekly Activity Mix ------------------------------------------
st.subheader("Weekly activity mix")

mix_df = con.execute(
    """
    SELECT
        DATE_TRUNC('week', start_time) AS week,
        CASE
            WHEN activity_type = 'running' THEN 'Running'
            WHEN activity_type = 'cycling' THEN 'Cycling'
            WHEN activity_type IN ('strength_training', 'functional_strength_training',
                                    'traditional_strength_training') THEN 'Strength'
            ELSE 'Other'
        END AS category,
        COUNT(*) AS sessions
    FROM workouts
    WHERE start_time >= $1
      AND start_time < $2
    GROUP BY DATE_TRUNC('week', start_time), category
    ORDER BY week, category
    """,
    [training_start, race_date],
).fetchdf()

if mix_df.empty:
    st.info("No workout data in this training window.")
else:
    mix_df["week"] = pd.to_datetime(mix_df["week"], utc=True)
    mix_df["wk_label"] = mix_df["week"].apply(lambda w: _weeks_to_race(w, race_date))

    category_colors = {
        "Running": "steelblue",
        "Cycling": "orange",
        "Strength": "mediumpurple",
        "Other": "lightgray",
    }
    fig_mix = go.Figure()
    for cat in ["Running", "Cycling", "Strength", "Other"]:
        cat_data = mix_df[mix_df["category"] == cat]
        if not cat_data.empty:
            fig_mix.add_trace(
                go.Bar(
                    x=cat_data["wk_label"],
                    y=cat_data["sessions"],
                    name=cat,
                    marker_color=category_colors[cat],
                )
            )
    fig_mix.update_layout(
        barmode="stack",
        height=350,
        margin={"l": 40, "r": 20, "t": 20, "b": 30},
        xaxis_title="Weeks to race",
        yaxis_title="Sessions",
        legend={"orientation": "h", "y": -0.2},
        hovermode="x unified",
    )
    st.plotly_chart(fig_mix, use_container_width=True)

# --- Panel 6: VO2 Max Trend -----------------------------------------------
st.subheader("VO2 max trend")

vo2_df = con.execute(
    """
    SELECT start_time, value AS vo2max
    FROM health_metrics
    WHERE metric = 'vo2max'
      AND start_time >= $1
      AND start_time < $2
    ORDER BY start_time
    """,
    [training_start, race_date],
).fetchdf()

if vo2_df.empty:
    st.info("No VO2 max data available for this training window.")
else:
    vo2_df["start_time"] = pd.to_datetime(vo2_df["start_time"], utc=True)

    fig_vo2 = go.Figure()
    fig_vo2.add_trace(
        go.Scatter(
            x=vo2_df["start_time"],
            y=vo2_df["vo2max"],
            mode="lines+markers",
            line={"color": "teal", "width": 2},
            marker={"size": 5},
        )
    )
    fig_vo2.update_layout(
        height=300,
        margin={"l": 40, "r": 20, "t": 20, "b": 30},
        xaxis_title="Date",
        yaxis_title="VO2 max (mL/kg/min)",
        showlegend=False,
        hovermode="x unified",
    )
    st.plotly_chart(fig_vo2, use_container_width=True)
