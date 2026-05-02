# CLAUDE.md

Guidance for Claude Code working on this repo. Read `DESIGN.md` for architecture and rationale.

## What this project is

A personal local-first fitness dashboard. Apple Health export → DuckDB → Streamlit. See `DESIGN.md` for layers and data model.

## Conventions

- **Python 3.11+**. Use `uv` for dependency management (`uv add`, `uv run`).
- **Type hints everywhere.** Use `from __future__ import annotations`. Run `mypy` if in doubt.
- **Format with `ruff`**, lint with `ruff check`. No bikeshedding on style.
- **Prefer `polars` over `pandas`** for new code unless interop forces pandas (Streamlit charts, some libraries). Polars is faster and the API is cleaner. Existing pandas code is fine, don't churn.
- **DuckDB is the source of truth.** Don't write intermediate CSVs/Parquets unless there's a specific reason. Query DuckDB directly.
- **Idempotent ingest.** Use `INSERT OR REPLACE` / `MERGE` patterns. Re-running ingest on the same export must not duplicate rows.
- **Stable IDs.** `workout_id` is a hash of `(source, source_id)` or `(source, start_time, activity_type)` if no source_id. Define this once in `src/storage/ids.py` and use it everywhere.

## Layout

```
src/
  ingest/     # raw → normalized records
  storage/    # DuckDB schema, migrations, helpers
  enrich/     # weather, training load — runs after ingest
  app/        # Streamlit; pages are disposable
tests/
  fixtures/   # small anonymized sample data, OK to commit
data/         # .gitignored — real data lives here
notebooks/    # one-off explorations, optional
```

## Data location

- Raw exports live OUTSIDE the repo (e.g., `~/fitness-data/raw/`). Do not commit `export.zip`, `export.xml`, or any GPX/FIT files containing real data.
- The DuckDB file lives at `data/fitness.duckdb` (gitignored).
- Test fixtures in `tests/fixtures/` are small, anonymized, and safe to commit.

## What to do, what to ask

- **Just do it:** writing parsers, adding columns to schema, adding a Streamlit page, fixing bugs, writing tests, refactoring within a layer.
- **Ask first:** changing the data model in a way that requires a migration, adding a new external dependency (especially anything that needs an API key), changing the layer boundaries described in `DESIGN.md`.

## Testing

- Unit tests for parsers should run against fixtures in `tests/fixtures/`, not against real export data.
- When adding a parser, add at least one fixture (anonymized — strip names, round coordinates).
- `pytest` is the runner. Tests live in `tests/` mirroring `src/` layout.

## Running things

- `uv run python -m src.ingest.apple_health <path-to-export.zip>` — ingest a fresh export
- `uv run python -m src.enrich.weather` — backfill weather
- `uv run python -m src.enrich.training_load` — recompute training load
- `uv run streamlit run src/app/Home.py` — launch dashboard
- `make refresh` — runs ingest + enrich (once Makefile exists)

## Display units

- **Distance is always in miles.** DuckDB stores `distance_m` in meters; convert to miles (`distance_m / 1609.344`) at the display layer. Never show km in dashboards.

## Things to be careful about

- **The Apple Health XML can be hundreds of MB.** Stream-parse with `lxml.etree.iterparse`, don't load it all into memory.
- **Timestamps are timezone-aware.** Apple Health uses local time with offset. Store as UTC in DuckDB; convert at the edges.
- **Workout activity types are inconsistent.** "HKWorkoutActivityTypeRunning" vs. "Running" vs. "running". Normalize on ingest using a single mapping in `src/ingest/activity_types.py`.
- **Don't trust calorie counts.** They're estimates and vary wildly across sources. Display them but don't build core analysis around them.
- **Open-Meteo rate limits.** Cache aggressively. Don't refetch weather for a workout that already has it unless explicitly asked.

## When in doubt

Read `DESIGN.md`. If it doesn't answer the question, the question probably needs a decision — surface it rather than guessing.
