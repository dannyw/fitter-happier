# Fitness Dashboard — Technical Design

## What this is

A personal, local-first fitness dashboard that ingests Apple Health and Strava export data, normalizes it into a queryable database, and presents trends, correlations, and goal tracking through a lightweight web UI.

The point is not to build a polished product. The point is to have a durable data layer that lets me ask new questions cheaply, and a UI thin enough that throwing it away and rewriting it is not a big deal.

## Design principles

1. **The data layer is the investment. The UI is disposable.** Schema changes are expensive; chart changes are not. Optimize accordingly.
2. **Local-first.** Runs on my Mac. No auth, no hosting, no cloud dependencies for the core loop. Weather backfill is the one external API.
3. **Treat "a workout" as the unit of ingest, regardless of source.** Today it comes from an Apple Health XML export. Tomorrow it might come from a Strava webhook. The downstream pipeline shouldn't care.
4. **Idempotent ingest.** Re-running ingest on the same data should produce the same result. New data adds, existing data updates in place. No duplicates.
5. **Lean on validated open source.** Don't reinvent FIT parsing, training-load math, or charting.

## Architecture

Three layers, kept separate:

```
   raw exports (XML, GPX, Strava archive)
              │
              ▼
   ┌──────────────────────┐
   │  INGEST              │  src/ingest/
   │  parse → normalize   │  Apple Health XML, GPX, FIT, TCX, Strava CSV
   └──────────────────────┘
              │
              ▼
   ┌──────────────────────┐
   │  STORAGE             │  src/storage/
   │  DuckDB (local file) │  workouts, samples, health_metrics, routes
   └──────────────────────┘
              │
              ▼
   ┌──────────────────────┐
   │  ENRICH              │  src/enrich/
   │  weather, training   │  Open-Meteo backfill, CTL/ATL/TSB
   │  load, derived       │
   └──────────────────────┘
              │
              ▼
   ┌──────────────────────┐
   │  APP                 │  src/app/
   │  Streamlit           │  pages = views; cheap to add/remove
   └──────────────────────┘
```

### Ingest layer

Responsibility: read a raw export, produce normalized records ready for storage. No analysis, no derivation beyond what's necessary to normalize.

**Apple Health XML** (`src/ingest/apple_health.py`):
- Parses `export.xml` from the Apple Health iOS export
- Extracts `<Workout>` records → workout summaries
- Extracts samples (`HeartRate`, `HKQuantityTypeIdentifierHeartRateVariabilitySDNN`, `SleepAnalysis`, `BodyMass`, `VO2Max`, `RestingHeartRate`, etc.) → health metrics
- Joins workout-overlapping samples (HR during a run) → workout samples
- Reads `workout-routes/*.gpx` → route geometry

**GPX** (`src/ingest/gpx.py`):
- Parses GPX route files (used by Apple Health export and as a general fallback)
- Returns lat/lon/elevation/timestamp tracks

**FIT** (`src/ingest/fit.py`):
- Parses FIT activity files (binary format used by Garmin, Strava exports) via `fitdecode`
- Handles `.fit` and `.fit.gz` transparently
- Extracts GPS (semicircles → degrees), altitude, heart rate, cadence, power, speed
- Returns `list[RoutePoint]` and `list[FitSample]`

**TCX** (`src/ingest/tcx.py`):
- Parses TCX (Training Center XML) activity files via `lxml`
- Handles `.tcx` and `.tcx.gz` transparently
- Extracts GPS, elevation, heart rate, cadence from `<Trackpoint>` elements
- Returns `list[RoutePoint]` and `list[FitSample]`

**Strava** (`src/ingest/strava.py`):
- Ingests Strava bulk export archive (no API key needed)
- Parses `activities.csv` with Polars (all columns read as strings for robustness)
- For each activity, checks for an existing Apple Health workout match (same activity type + start time within 5 min + distance within 20%)
- **Match found → enrich**: merges Strava metadata (`strava_activity_name`, `strava_description`, `strava_gear`, `strava_activity_id`) into the existing workout's `raw` JSON; backfills route/sample data from activity files if the existing record has none
- **No match → insert**: new workout row with `source="strava"`, full CSV row in `raw` JSON; dispatches to FIT/GPX/TCX parsers for activity files
- Run order: Apple Health first, then Strava. Re-running is idempotent

**Activity type normalization** (`src/ingest/activity_types.py`):
- Maps Apple Health identifiers (`HKWorkoutActivityTypeRunning`) and Strava types (`Run`, `TrailRun`, `Ride`, etc.) to a shared set of lowercase names (`running`, `cycling`, etc.)
- `normalize_activity_type()` for Apple Health, `normalize_strava_activity_type()` for Strava

### Storage layer

DuckDB, single file at `data/fitness.duckdb`. Why DuckDB: zero-config, file-based, fast for analytical queries, reads Parquet/CSV directly, plays well with pandas/polars.

Schema (initial):

```sql
-- One row per workout session
workouts (
  workout_id        TEXT PRIMARY KEY,    -- stable hash of source + start_time
  source            TEXT,                 -- 'apple_health' | 'strava' | ...
  source_id         TEXT,                 -- original ID from source
  activity_type     TEXT,                 -- 'running', 'tennis', 'pilates', ...
  start_time        TIMESTAMP,
  end_time          TIMESTAMP,
  duration_sec      INTEGER,
  distance_m        DOUBLE,               -- nullable (no distance for Pilates)
  energy_kcal       DOUBLE,
  avg_hr            DOUBLE,
  max_hr            DOUBLE,
  elevation_gain_m  DOUBLE,
  device            TEXT,
  raw               JSON                  -- everything else, for future use
);

-- High-frequency samples during a workout
workout_samples (
  workout_id   TEXT,
  timestamp    TIMESTAMP,
  metric       TEXT,                     -- 'heart_rate', 'pace', 'cadence', ...
  value        DOUBLE,
  PRIMARY KEY (workout_id, timestamp, metric)
);

-- Non-workout health data: sleep, HRV, weight, RHR, VO2max
health_metrics (
  metric       TEXT,
  start_time   TIMESTAMP,
  end_time     TIMESTAMP,
  value        DOUBLE,
  unit         TEXT,
  source       TEXT,
  PRIMARY KEY (metric, start_time, source)
);

-- GPS route points
routes (
  workout_id   TEXT,
  timestamp    TIMESTAMP,
  lat          DOUBLE,
  lon          DOUBLE,
  elevation_m  DOUBLE,
  PRIMARY KEY (workout_id, timestamp)
);

-- Weather conditions at workout start (one row per outdoor workout)
weather (
  workout_id        TEXT PRIMARY KEY,
  temp_c            DOUBLE,
  humidity_pct      DOUBLE,
  wind_speed_mps    DOUBLE,
  precipitation_mm  DOUBLE,
  conditions        TEXT
);
```

Schema migrations: keep migrations as numbered SQL files in `src/storage/migrations/`. On startup, the app applies any pending migrations to the DuckDB file.

### Enrich layer

Computed *after* ingest, idempotent. Each enricher reads from storage and writes back derived data.

**Weather** (`src/enrich/weather.py`):
- For each outdoor workout (has route data), call Open-Meteo historical API with start lat/lon and start time
- Cache by (lat rounded, lon rounded, hour) — many runs from same neighborhood share weather
- Open-Meteo: free, no key, rate limit is generous

**Training load** (`src/enrich/training_load.py`):
- Compute TRIMP (HR-based load) per workout
- Roll up into CTL (42-day exponentially weighted), ATL (7-day), TSB (CTL - ATL)
- Stored as a daily series, recomputed on new ingest

### App layer

Streamlit (`src/app/`). Each `.py` file in `src/app/pages/` is a page. To add a view: write a file. To remove a view: delete a file.

Initial pages:
- `1_overview.py` — recent activity summary, weekly volume
- `2_running_trends.py` — pace, HR, distance over time; pace-at-HR drift
- `3_correlations.py` — sleep vs. next-day workout HR; weather vs. pace
- `4_training_load.py` — CTL/ATL/TSB chart

The dashboard is read-only against the DuckDB file. Ingest and enrichment are run as separate commands (initially manually, eventually on a cron / file-watch).

## Data flow

```
1. Drop Apple Health export.zip into ~/fitness-data/raw/
2. Run: python -m src.ingest.apple_health ~/fitness-data/raw/export.zip
   → parses XML, populates workouts / workout_samples / health_metrics / routes
3. (Optional) Drop Strava archive into ~/fitness-data/raw/
4. Run: python -m src.ingest.strava ~/fitness-data/raw/strava-export.zip
   → parses CSV + activity files, enriches overlapping workouts, inserts new ones
5. Run: python -m src.enrich.weather
   → backfills weather for all outdoor workouts
6. Run: python -m src.enrich.training_load
   → recomputes daily training load series
7. Run: streamlit run src/app/Home.py
   → opens dashboard in browser at localhost:8501
```

A single `make refresh` (or `just refresh`) target wraps steps 2–6. Apple Health must run before Strava so the merge logic can find existing workouts to enrich.

## Decisions resolved

- **Strava sync.** Went with bulk export + merge-at-ingest instead of API sync. No API key needed; user downloads their archive from strava.com/account and runs a CLI command. Merge logic deduplicates overlapping workouts and enriches Apple Health records with Strava metadata.
- **FIT files.** Added `fitdecode`-based parser. Used by Strava ingest for `.fit.gz` activity files; also available standalone.

## Decisions deferred

- **Strava API sync.** Could replace bulk export for incremental updates. The schema and merge logic already support it — just needs an OAuth flow and polling.
- **Phone access to dashboard.** Tailscale + Mac-as-host is the obvious path. Don't build until wanted.
- **Subjective notes per workout.** Not for v1. If added later: a `notes/` directory of markdown files, one per workout, parsed in.
- **Goal tracking UI.** Need to use the dashboard for a few weeks before designing this — what "a goal" means depends on what I actually train for.

## Non-goals

- Multi-user. This is mine.
- Real-time. Daily refresh is fine.
- Mobile-native. Web on Mac is the target.
- Pretty design. Streamlit defaults are fine until they aren't.
