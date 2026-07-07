# Fitness dashboard tasks. Raw exports live outside the repo (see CLAUDE.md);
# override paths on the command line, e.g. `make ingest-apple EXPORT=~/foo.zip`.

APPLE_EXPORT ?= ~/fitness-data/raw/export.zip
STRAVA_EXPORT ?= ~/fitness-data/raw/strava-export.zip

.PHONY: refresh ingest-apple ingest-strava strava-auth strava-sync reconcile enrich dashboard test

## refresh: full pipeline — ingest both sources, reconcile, enrich
# Apple Health must run before Strava so the merge logic can enrich existing
# workouts; reconcile then cleans up any Strava-first duplicates before the
# derived enrichers compute against the de-duplicated set.
refresh: ingest-apple ingest-strava reconcile enrich

ingest-apple:
	uv run python -m src.ingest.apple_health $(APPLE_EXPORT)

ingest-strava:
	uv run python -m src.ingest.strava $(STRAVA_EXPORT)

## strava-auth: one-time Strava OAuth (opens a browser); needs a registered app
strava-auth:
	uv run python -m src.ingest.strava_auth

## strava-sync: incremental Strava sync over the REST API (requires strava-auth)
strava-sync:
	uv run python -m src.ingest.strava_api

reconcile:
	uv run python -m src.enrich.reconcile_sources

enrich:
	uv run python -m src.enrich.weather --backfill
	uv run python -m src.enrich.training_load

dashboard:
	uv run streamlit run src/app/Home.py

test:
	uv run pytest
