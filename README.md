# Fitness Dashboard

Personal local-first fitness dashboard. Apple Health export → DuckDB → Streamlit.

See [`DESIGN.md`](./DESIGN.md) for architecture. See [`CLAUDE.md`](./CLAUDE.md) for development conventions.

## Quick start

```bash
# Install dependencies
uv sync

# Ingest an Apple Health export
uv run python -m src.ingest.apple_health ~/fitness-data/raw/export.zip

# Enrich
uv run python -m src.enrich.weather
uv run python -m src.enrich.training_load

# Launch dashboard
uv run streamlit run src/app/Home.py
```

## Status

v1 in progress. See `DESIGN.md` for scope.
