"""Training Overview — year-by-year heatmap, cumulative mileage, fitness trend."""

from __future__ import annotations

import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DB_PATH = Path("data/fitness.duckdb")

st.set_page_config(page_title="Training Overview", layout="wide")
st.title("Training Overview")


@st.cache_resource
def get_con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


con = get_con()

current_year = datetime.date.today().year

# ===========================================================================
# 1. Metric cards — current year snapshot
# ===========================================================================
ytd = con.execute(
    """
    SELECT
        COALESCE(SUM(distance_m) / 1609.344, 0) AS ytd_miles,
        COUNT(*) AS runs,
        COALESCE(MAX(distance_m) / 1609.344, 0) AS longest_run
    FROM workouts
    WHERE activity_type = 'running'
      AND EXTRACT(YEAR FROM start_time) = $1
      AND distance_m > 0
    """,
    [current_year],
).fetchone()

ytd_miles, num_runs, longest_run = ytd

# Weeks elapsed this year (at least 1 to avoid division by zero)
day_of_year = datetime.date.today().timetuple().tm_yday
weeks_elapsed = max(day_of_year / 7, 1)
avg_weekly = ytd_miles / weeks_elapsed

c1, c2, c3, c4 = st.columns(4)
c1.metric("YTD Miles", f"{ytd_miles:,.0f}")
c2.metric("Runs", f"{num_runs:,}")
c3.metric("Avg Weekly Miles", f"{avg_weekly:.1f}")
c4.metric("Longest Run", f"{longest_run:.1f} mi")

# ===========================================================================
# 2. Weekly mileage heatmap — year by year (hero section)
# ===========================================================================
st.subheader("Weekly mileage heatmap")

weekly_df = con.execute(
    """
    SELECT
        EXTRACT(ISOYEAR FROM start_time)::INT AS yr,
        EXTRACT(WEEK FROM start_time)::INT AS wk,
        SUM(distance_m) / 1609.344 AS miles
    FROM workouts
    WHERE activity_type = 'running'
      AND distance_m > 0
    GROUP BY yr, wk
    ORDER BY yr, wk
    """
).fetchdf()

if weekly_df.empty:
    st.info("No running data available.")
    st.stop()

years = sorted(weekly_df["yr"].unique())
weeks = list(range(1, 53))

# Build a 2D array: rows = years (earliest at top), cols = weeks 1-52
z = np.zeros((len(years), 52))
for _, row in weekly_df.iterrows():
    yr_idx = years.index(int(row["yr"]))
    wk_idx = int(row["wk"]) - 1
    if 0 <= wk_idx < 52:
        z[yr_idx, wk_idx] = row["miles"]

# Reverse: Plotly renders first z-row at the bottom, so put most recent first
years_ordered = list(reversed(years))
z_ordered = z[::-1]

# Annual totals
annual_totals = {yr: z[i].sum() for i, yr in enumerate(years)}
pr_year = max(annual_totals, key=annual_totals.get)

# Month labels at approximate week positions
month_ticks = [
    (1, "Jan"), (5, "Feb"), (9, "Mar"), (14, "Apr"), (18, "May"), (22, "Jun"),
    (27, "Jul"), (31, "Aug"), (35, "Sep"), (40, "Oct"), (44, "Nov"), (48, "Dec"),
]

# Hover text
hover_text = []
for yr in years_ordered:
    row_text = []
    for wk in weeks:
        yr_idx = years.index(yr)
        miles = z[yr_idx, wk - 1]
        row_text.append(f"Year: {yr}<br>Week: {wk}<br>Miles: {miles:.1f}")
    hover_text.append(row_text)

# Build year labels with annual totals
y_labels = []
for yr in years_ordered:
    total = annual_totals[yr]
    suffix = ""
    if yr == current_year:
        suffix = "*"
    if yr == pr_year:
        suffix += " \u2605"  # star
    y_labels.append(f"{yr}  ({total:,.0f} mi{suffix})")

fig_heat = go.Figure(
    data=go.Heatmap(
        z=z_ordered,
        x=weeks,
        y=y_labels,
        colorscale=[
            [0, "#f5f0eb"],
            [0.25, "#e0c9b0"],
            [0.5, "#c49a6c"],
            [0.75, "#9a6840"],
            [1, "#6b3a2a"],
        ],
        hovertext=hover_text,
        hovertemplate="%{hovertext}<extra></extra>",
        showscale=True,
        colorbar={"title": "Miles", "thickness": 15},
        zmin=0,
    )
)

fig_heat.update_layout(
    height=max(400, len(years) * 40 + 100),
    margin={"l": 20, "r": 20, "t": 20, "b": 40},
    xaxis={
        "title": "",
        "tickvals": [t[0] for t in month_ticks],
        "ticktext": [t[1] for t in month_ticks],
        "side": "bottom",
    },
    yaxis={"title": ""},
)

st.plotly_chart(fig_heat, use_container_width=True)

# ===========================================================================
# 3. Weekly mileage comparison — last 3 years
# ===========================================================================
st.subheader("Weekly mileage — year over year")

recent_years = sorted(weekly_df["yr"].unique())[-3:]
year_colors = {
    recent_years[-3]: "gray" if len(recent_years) >= 3 else "mediumseagreen",
    recent_years[-2]: "cornflowerblue" if len(recent_years) >= 2 else "mediumseagreen",
    recent_years[-1]: "mediumseagreen",
}

fig_weekly = go.Figure()
for yr in recent_years:
    # Build full 52-week series with zeros for missing weeks
    yr_data = weekly_df[weekly_df["yr"] == yr][["wk", "miles"]].copy()
    full_weeks = pd.DataFrame({"wk": range(1, 53)})
    yr_data = full_weeks.merge(yr_data, on="wk", how="left").fillna(0).sort_values("wk")

    # Trim future weeks for current year
    if int(yr) == current_year:
        current_week = datetime.date.today().isocalendar()[1]
        yr_data = yr_data[yr_data["wk"] <= current_week]

    fig_weekly.add_trace(
        go.Scatter(
            x=yr_data["wk"],
            y=yr_data["miles"],
            mode="lines",
            name=str(int(yr)),
            line={"color": year_colors[yr], "width": 2.5 if yr == recent_years[-1] else 1.5},
            hovertemplate=f"{int(yr)}<br>Week %{{x}}<br>%{{y:.1f}} mi<extra></extra>",
        )
    )

fig_weekly.update_layout(
    height=350,
    margin={"l": 40, "r": 20, "t": 20, "b": 30},
    xaxis={
        "title": "",
        "tickvals": [t[0] for t in month_ticks],
        "ticktext": [t[1] for t in month_ticks],
    },
    yaxis_title="Miles / week",
    legend={"orientation": "h", "y": -0.15},
    hovermode="x unified",
)
st.plotly_chart(fig_weekly, use_container_width=True)

# ===========================================================================
# 4. Year-over-year cumulative mileage
# ===========================================================================
st.subheader("Cumulative mileage by year")

cum_df = con.execute(
    """
    SELECT
        CAST(start_time AS DATE) AS dt,
        EXTRACT(YEAR FROM start_time)::INT AS yr,
        EXTRACT(DOY FROM start_time)::INT AS doy,
        distance_m / 1609.344 AS miles
    FROM workouts
    WHERE activity_type = 'running'
      AND distance_m > 0
    ORDER BY dt
    """
).fetchdf()

if not cum_df.empty:
    # Aggregate daily miles per year, then cumsum
    daily = cum_df.groupby(["yr", "doy"])["miles"].sum().reset_index()

    # Color scheme: gray for older years, distinct colors for recent
    color_palette = {
        0: "lightgray",      # oldest
        -3: "gray",
        -2: "orange",
        -1: "cornflowerblue",
    }
    cum_years = sorted(daily["yr"].unique())

    fig_cum = go.Figure()
    for yr in cum_years:
        yr_data = daily[daily["yr"] == yr].sort_values("doy")
        # Fill in all days 1-365
        all_days = pd.DataFrame({"doy": range(1, 366)})
        yr_data = all_days.merge(yr_data, on="doy", how="left")
        yr_data["miles"] = yr_data["miles"].fillna(0)
        yr_data["cum_miles"] = yr_data["miles"].cumsum()
        # Only plot up to last nonzero day for current/partial years
        last_day = yr_data[yr_data["miles"] > 0]["doy"].max()
        if pd.isna(last_day):
            continue
        yr_data = yr_data[yr_data["doy"] <= last_day]

        rank = cum_years.index(yr) - len(cum_years)  # negative index from end
        if rank <= -4:
            color = "lightgray"
            width = 1
        elif rank == -3:
            color = "gray"
            width = 1.5
        elif rank == -2:
            color = "orange"
            width = 2
        elif rank == -1:
            color = "cornflowerblue"
            width = 2
        else:
            color = "mediumseagreen"
            width = 2.5

        # Most recent year gets the boldest treatment
        if yr == cum_years[-1]:
            color = "mediumseagreen"
            width = 2.5

        fig_cum.add_trace(
            go.Scatter(
                x=yr_data["doy"],
                y=yr_data["cum_miles"],
                mode="lines",
                name=str(yr),
                line={"color": color, "width": width},
                hovertemplate=f"{yr}<br>Day %{{x}}<br>%{{y:.0f}} miles<extra></extra>",
            )
        )

    # Month labels on x-axis
    month_starts = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335]
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    fig_cum.update_layout(
        height=400,
        margin={"l": 40, "r": 20, "t": 20, "b": 30},
        xaxis={
            "title": "",
            "tickvals": month_starts,
            "ticktext": month_names,
        },
        yaxis_title="Cumulative miles",
        legend={"orientation": "h", "y": -0.15},
        hovermode="x unified",
    )
    st.plotly_chart(fig_cum, use_container_width=True)

# ===========================================================================
# 4. Fitness trend (CTL) — long range
# ===========================================================================
st.subheader("Fitness trend (CTL)")

tl_df = con.execute(
    "SELECT date, ctl, atl FROM training_load ORDER BY date"
).fetchdf()

if tl_df.empty:
    st.info("No training load data available.")
else:
    tl_df["date"] = pd.to_datetime(tl_df["date"])

    fig_ctl = go.Figure()
    fig_ctl.add_trace(
        go.Scatter(
            x=tl_df["date"],
            y=tl_df["atl"],
            mode="lines",
            line={"color": "orange", "width": 1},
            name="Fatigue (ATL)",
            opacity=0.5,
        )
    )
    fig_ctl.add_trace(
        go.Scatter(
            x=tl_df["date"],
            y=tl_df["ctl"],
            mode="lines",
            line={"color": "royalblue", "width": 2},
            name="Fitness (CTL)",
        )
    )
    fig_ctl.update_layout(
        height=300,
        margin={"l": 40, "r": 20, "t": 20, "b": 30},
        yaxis_title="CTL / ATL",
        legend={"orientation": "h", "y": -0.15},
        hovermode="x unified",
    )
    st.plotly_chart(fig_ctl, use_container_width=True)
