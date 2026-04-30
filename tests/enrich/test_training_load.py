"""Tests for src.enrich.training_load — TRIMP + CTL/ATL/TSB computation."""

from __future__ import annotations

from datetime import date, timedelta, timezone, datetime

import duckdb
import pytest

from src.enrich.training_load import (
    compute_trimp,
    enrich_training_load_con,
    _compute_ctl_atl_tsb,
)
from src.storage.schema import ensure_schema


class TestComputeTrimp:
    def test_moderate_run(self) -> None:
        """30 min at ~70% HR reserve should give a reasonable TRIMP."""
        trimp = compute_trimp(
            duration_sec=1800, avg_hr=140, max_hr=190, resting_hr=60
        )
        # HRr = (140-60)/(190-60) ≈ 0.615
        # TRIMP = 30 * 0.615 * 0.64 * e^(1.92*0.615) ≈ 30 * 0.615 * 0.64 * 3.27 ≈ 38.6
        assert trimp == pytest.approx(38.6, abs=1.0)

    def test_zero_duration(self) -> None:
        trimp = compute_trimp(
            duration_sec=0, avg_hr=140, max_hr=190, resting_hr=60
        )
        assert trimp == 0.0

    def test_avg_below_resting(self) -> None:
        trimp = compute_trimp(
            duration_sec=1800, avg_hr=55, max_hr=190, resting_hr=60
        )
        assert trimp == 0.0

    def test_avg_equals_resting(self) -> None:
        trimp = compute_trimp(
            duration_sec=1800, avg_hr=60, max_hr=190, resting_hr=60
        )
        assert trimp == 0.0

    def test_max_below_resting(self) -> None:
        trimp = compute_trimp(
            duration_sec=1800, avg_hr=140, max_hr=50, resting_hr=60
        )
        assert trimp == 0.0

    def test_high_intensity(self) -> None:
        """60 min at ~85% HR reserve — long hard effort."""
        trimp = compute_trimp(
            duration_sec=3600, avg_hr=170, max_hr=190, resting_hr=60
        )
        # HRr = 110/130 ≈ 0.846
        # TRIMP = 60 * 0.846 * 0.64 * e^(1.92*0.846) ≈ 60 * 0.846 * 0.64 * 5.08 ≈ 164
        assert trimp > 100

    def test_avg_exceeds_max(self) -> None:
        """avg_hr > max_hr should clamp HR reserve to 1.0."""
        trimp = compute_trimp(
            duration_sec=1800, avg_hr=200, max_hr=190, resting_hr=60
        )
        # HRr clamped to 1.0
        # TRIMP = 30 * 1.0 * 0.64 * e^1.92 ≈ 30 * 0.64 * 6.82 ≈ 131
        assert trimp == pytest.approx(130.9, abs=1.0)


class TestCtlAtlTsb:
    def test_empty(self) -> None:
        assert _compute_ctl_atl_tsb({}) == []

    def test_single_day(self) -> None:
        rows = _compute_ctl_atl_tsb({date(2024, 1, 1): 50.0})
        assert len(rows) == 1
        d, trimp, ctl, atl, tsb = rows[0]
        assert d == date(2024, 1, 1)
        assert trimp == 50.0
        assert ctl > 0
        assert atl > 0
        assert tsb == pytest.approx(ctl - atl)

    def test_atl_responds_faster(self) -> None:
        """ATL should rise faster than CTL for a sudden load spike."""
        daily = {date(2024, 1, 1): 100.0}
        # Fill rest days
        for i in range(1, 10):
            daily[date(2024, 1, 1) + timedelta(days=i)] = 0.0

        rows = _compute_ctl_atl_tsb(daily)
        # After day 1 with 100 TRIMP, ATL should be higher than CTL
        _, _, ctl_1, atl_1, _ = rows[0]
        assert atl_1 > ctl_1  # ATL reacts faster to single spike

    def test_contiguous_dates(self) -> None:
        """Result should cover every date from first to last workout."""
        daily = {
            date(2024, 1, 1): 50.0,
            date(2024, 1, 5): 30.0,
        }
        rows = _compute_ctl_atl_tsb(daily)
        assert len(rows) == 5  # Jan 1–5 inclusive
        dates = [r[0] for r in rows]
        assert dates[0] == date(2024, 1, 1)
        assert dates[-1] == date(2024, 1, 5)

    def test_rest_days_have_zero_trimp(self) -> None:
        daily = {
            date(2024, 1, 1): 50.0,
            date(2024, 1, 3): 30.0,
        }
        rows = _compute_ctl_atl_tsb(daily)
        # Jan 2 should have trimp=0
        assert rows[1][1] == 0.0


class TestEnrichIntegration:
    @pytest.fixture()
    def con(self) -> duckdb.DuckDBPyConnection:
        c = duckdb.connect(":memory:")
        ensure_schema(c)
        yield c
        c.close()

    def test_basic_enrichment(self, con: duckdb.DuckDBPyConnection) -> None:
        """Insert workouts and verify training_load table is populated."""
        con.execute(
            """
            INSERT INTO workouts (workout_id, source, activity_type,
                                  start_time, duration_sec, avg_hr, max_hr)
            VALUES
                ('w1', 'apple_health', 'running',
                 '2024-01-01 08:00:00+00', 1800, 140, 175),
                ('w2', 'apple_health', 'running',
                 '2024-01-03 08:00:00+00', 2700, 150, 180)
            """
        )

        count = enrich_training_load_con(con)
        assert count == 3  # Jan 1, 2, 3

        rows = con.execute(
            "SELECT date, trimp, ctl, atl, tsb FROM training_load ORDER BY date"
        ).fetchall()
        assert len(rows) == 3

        # Day 1 should have non-zero TRIMP
        assert rows[0][1] > 0
        # Day 2 (rest) should have zero TRIMP
        assert rows[1][1] == 0.0
        # CTL/ATL should be positive after workouts
        assert rows[2][2] > 0  # ctl
        assert rows[2][3] > 0  # atl

    def test_no_hr_workouts_skipped(self, con: duckdb.DuckDBPyConnection) -> None:
        """Workouts without avg_hr should not contribute TRIMP."""
        con.execute(
            """
            INSERT INTO workouts (workout_id, source, activity_type,
                                  start_time, duration_sec, avg_hr, max_hr)
            VALUES ('w1', 'apple_health', 'cycling',
                    '2024-01-01 08:00:00+00', 3600, NULL, NULL)
            """
        )

        count = enrich_training_load_con(con)
        assert count == 0  # No workouts with HR → nothing to write

    def test_idempotent(self, con: duckdb.DuckDBPyConnection) -> None:
        """Running enrichment twice should produce same results."""
        con.execute(
            """
            INSERT INTO workouts (workout_id, source, activity_type,
                                  start_time, duration_sec, avg_hr, max_hr)
            VALUES ('w1', 'apple_health', 'running',
                    '2024-01-01 08:00:00+00', 1800, 140, 175)
            """
        )

        enrich_training_load_con(con)
        enrich_training_load_con(con)

        count = con.execute("SELECT count(*) FROM training_load").fetchone()[0]
        assert count == 1

    def test_uses_resting_hr_from_health_metrics(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        """When resting HR is available in health_metrics, use it."""
        # Insert resting HR records
        con.execute(
            """
            INSERT INTO health_metrics (metric, start_time, end_time, value, unit, source)
            VALUES
                ('resting_heart_rate', '2024-01-01', '2024-01-01', 52.0, 'bpm', 'apple'),
                ('resting_heart_rate', '2024-01-02', '2024-01-02', 54.0, 'bpm', 'apple'),
                ('resting_heart_rate', '2024-01-03', '2024-01-03', 50.0, 'bpm', 'apple')
            """
        )
        # Insert a workout
        con.execute(
            """
            INSERT INTO workouts (workout_id, source, activity_type,
                                  start_time, duration_sec, avg_hr, max_hr)
            VALUES ('w1', 'apple_health', 'running',
                    '2024-01-02 08:00:00+00', 1800, 140, 175)
            """
        )

        enrich_training_load_con(con)
        rows = con.execute("SELECT trimp FROM training_load WHERE trimp > 0").fetchall()
        assert len(rows) == 1

        # With resting HR 52 (median of 50, 52, 54), TRIMP should be higher
        # than with default 60 because the HR reserve fraction is larger
        trimp_with_real_rhr = rows[0][0]

        # Recompute with default resting HR for comparison
        from src.enrich.training_load import compute_trimp

        trimp_default = compute_trimp(1800, 140, 175, 60)
        trimp_real = compute_trimp(1800, 140, 175, 52)
        assert trimp_with_real_rhr == pytest.approx(trimp_real, abs=0.1)
        assert trimp_real > trimp_default
