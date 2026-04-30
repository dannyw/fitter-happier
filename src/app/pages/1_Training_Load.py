"""Training Load — fitness / fatigue chart."""

from __future__ import annotations

import datetime
from pathlib import Path

import duckdb
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

DB_PATH = Path("data/fitness.duckdb")

st.set_page_config(page_title="Training Load", layout="wide")
st.title("Training Load")


@st.cache_resource
def get_con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


con = get_con()

# --- Date range filter ---
today = datetime.date.today()
default_start = today - datetime.timedelta(days=90)

col_start, col_end = st.columns(2)
start_date = col_start.date_input("Start date", value=default_start)
end_date = col_end.date_input("End date", value=today)

# --- Query ---
df = con.execute(
    "SELECT * FROM training_load WHERE date BETWEEN ? AND ? ORDER BY date",
    [start_date, end_date],
).fetchdf()

if df.empty:
    st.info("No training load data in the selected range.")
    st.stop()

# --- Metric cards ---
latest = df.iloc[-1]
c1, c2, c3 = st.columns(3)
c1.metric("Fitness (CTL)", f"{latest['ctl']:.1f}")
c2.metric("Fatigue (ATL)", f"{latest['atl']:.1f}")
c3.metric("Form (TSB)", f"{latest['tsb']:.1f}")

# --- Main chart ---
fig = make_subplots(specs=[[{"secondary_y": True}]])

# TSB filled area — split into positive (green) and negative (red)
fig.add_trace(
    go.Scatter(
        x=df["date"],
        y=df["tsb"].clip(lower=0),
        fill="tozeroy",
        fillcolor="rgba(0,200,0,0.15)",
        line={"color": "rgba(0,200,0,0.4)", "width": 0},
        name="TSB (+)",
        showlegend=False,
    ),
    secondary_y=False,
)
fig.add_trace(
    go.Scatter(
        x=df["date"],
        y=df["tsb"].clip(upper=0),
        fill="tozeroy",
        fillcolor="rgba(200,0,0,0.15)",
        line={"color": "rgba(200,0,0,0.4)", "width": 0},
        name="TSB (-)",
        showlegend=False,
    ),
    secondary_y=False,
)

# TSB line (for legend)
fig.add_trace(
    go.Scatter(
        x=df["date"],
        y=df["tsb"],
        mode="lines",
        line={"color": "gray", "width": 1, "dash": "dot"},
        name="Form (TSB)",
    ),
    secondary_y=False,
)

# CTL
fig.add_trace(
    go.Scatter(
        x=df["date"],
        y=df["ctl"],
        mode="lines",
        line={"color": "royalblue", "width": 2},
        name="Fitness (CTL)",
    ),
    secondary_y=False,
)

# ATL
fig.add_trace(
    go.Scatter(
        x=df["date"],
        y=df["atl"],
        mode="lines",
        line={"color": "orange", "width": 2},
        name="Fatigue (ATL)",
    ),
    secondary_y=False,
)

# TRIMP bars
fig.add_trace(
    go.Bar(
        x=df["date"],
        y=df["trimp"],
        marker_color="rgba(150,150,150,0.4)",
        name="TRIMP",
    ),
    secondary_y=True,
)

fig.update_layout(
    height=500,
    margin={"l": 40, "r": 40, "t": 30, "b": 30},
    legend={"orientation": "h", "y": -0.15},
    barmode="overlay",
    hovermode="x unified",
)
fig.update_yaxes(title_text="CTL / ATL / TSB", secondary_y=False)
fig.update_yaxes(title_text="TRIMP", secondary_y=True)

st.plotly_chart(fig, use_container_width=True)

# --- Raw data ---
with st.expander("Raw data"):
    st.dataframe(df, use_container_width=True, hide_index=True)
