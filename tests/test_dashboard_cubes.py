"""Module M11 (+ M11-fix re-grain): the two dimensional gold cubes
(gold.web_funnel_daily_cube, gold.journey_daily_cube) backing the
filterable-KPI dashboard.

Parity is the load-bearing property here (per the M11 acceptance
criteria): with no filter, summing a cube over every dimension must
reproduce the pre-existing, unfiltered gold marts' totals exactly.

M11-fix: these cubes were originally week_start-grained; a real run of
"the last 3 days for Germany" against them silently produced a "last 6
weeks" dashboard, because a week bucket cannot answer a day-level filter.
Re-grained to day_date (see sql/medallion.sql's M11-fix comment) —
TestDailyCubeWeeklyRollupParity below additionally proves that grouping
the new daily cube back up to ISO weeks reproduces exactly what the
retired week_start cube's rows were, so the re-grain lost no information.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("driver")


class TestWebFunnelDailyCubeParity:
    def test_sum_test_starts_matches_funnel_overview(self, driver):
        cube = driver.query("SELECT SUM(test_starts) AS n FROM gold.web_funnel_daily_cube").iloc[0]["n"]
        mart = driver.query(
            "SELECT users FROM gold.funnel_overview WHERE stage = 'hearing_test_start'"
        ).iloc[0]["users"]
        assert cube == mart == 100000

    def test_sum_test_completes_matches_funnel_overview(self, driver):
        cube = driver.query("SELECT SUM(test_completes) AS n FROM gold.web_funnel_daily_cube").iloc[0]["n"]
        mart = driver.query(
            "SELECT users FROM gold.funnel_overview WHERE stage = 'hearing_test_complete'"
        ).iloc[0]["users"]
        assert cube == mart == 42062

    def test_sum_store_redirects_matches_linkable_share_by_market(self, driver):
        cube = driver.query("SELECT SUM(store_redirects) AS n FROM gold.web_funnel_daily_cube").iloc[0]["n"]
        mart = driver.query(
            "SELECT SUM(redirect_users) AS n FROM gold.linkable_share_by_market"
        ).iloc[0]["n"]
        assert cube == mart
        assert cube > 0

    def test_no_negative_measures(self, driver):
        df = driver.query(
            "SELECT test_starts, test_completes, store_redirects FROM gold.web_funnel_daily_cube"
        )
        assert (df >= 0).all().all()

    def test_row_count_equals_distinct_dimension_combinations(self, driver):
        n = driver.query("SELECT COUNT(*) AS n FROM gold.web_funnel_daily_cube").iloc[0]["n"]
        distinct_n = driver.query(
            "SELECT COUNT(*) AS n FROM (SELECT DISTINCT day_date, market, device_category, channel "
            "FROM gold.web_funnel_daily_cube) t"
        ).iloc[0]["n"]
        assert n == distinct_n


class TestJourneyDailyCubeParity:
    def test_sum_downloads_matches_downloads_by_channel(self, driver):
        cube = driver.query("SELECT SUM(downloads) AS n FROM gold.journey_daily_cube").iloc[0]["n"]
        mart = driver.query("SELECT SUM(linked_downloads) AS n FROM gold.downloads_by_channel").iloc[0]["n"]
        assert cube == mart
        assert cube > 0

    def test_sum_paired_users_matches_pairing_by_channel(self, driver):
        cube = driver.query("SELECT SUM(paired_users) AS n FROM gold.journey_daily_cube").iloc[0]["n"]
        mart = driver.query("SELECT SUM(paired_users) AS n FROM gold.pairing_by_channel").iloc[0]["n"]
        assert cube == mart
        assert cube > 0

    def test_per_channel_downloads_match_downloads_by_channel(self, driver):
        cube = driver.query(
            "SELECT acquisition_channel, SUM(downloads) AS n FROM gold.journey_daily_cube "
            "GROUP BY acquisition_channel ORDER BY acquisition_channel"
        ).reset_index(drop=True)
        mart = driver.query(
            "SELECT acquisition_channel, linked_downloads AS n FROM gold.downloads_by_channel "
            "ORDER BY acquisition_channel"
        ).reset_index(drop=True)
        assert (cube["n"].astype(float) == mart["n"].astype(float)).all()

    def test_d30_eligible_and_retained_sum_matches_d30_by_channel(self, driver):
        """On this dataset, silver.linked_journeys (the journey cube's
        source) happens to be the SAME population gold.d30_by_channel
        joins directly -- see the cube's COMMENT ON in sql/medallion.sql
        for why that is not guaranteed in general, only observed here."""
        cube = driver.query(
            "SELECT SUM(d30_eligible) AS e, SUM(d30_retained) AS r FROM gold.journey_daily_cube"
        ).iloc[0]
        mart = driver.query(
            "SELECT SUM(eligible_users) AS e, SUM(retained_users) AS r FROM gold.d30_by_channel"
        ).iloc[0]
        assert cube["e"] == mart["e"]
        assert cube["r"] == mart["r"]
        assert cube["e"] > 0

    def test_no_negative_measures(self, driver):
        df = driver.query(
            "SELECT downloads, paired_users, d30_eligible, d30_retained FROM gold.journey_daily_cube"
        )
        assert (df >= 0).all().all()

    def test_d30_eligible_never_exceeds_downloads(self, driver):
        df = driver.query("SELECT downloads, d30_eligible FROM gold.journey_daily_cube")
        assert (df["d30_eligible"] <= df["downloads"]).all()

    def test_d30_retained_never_exceeds_eligible(self, driver):
        df = driver.query("SELECT d30_eligible, d30_retained FROM gold.journey_daily_cube")
        assert (df["d30_retained"] <= df["d30_eligible"]).all()


class TestDailyCubeWeeklyRollupParity:
    """M11-fix regression guard: the re-grain from week_start to day_date
    must not have changed what a WEEKLY view of the data looks like — it
    only adds the ability to filter finer than a week. Grouping the daily
    cube's day_date up to ISO weeks (DATE_TRUNC('week', day_date)) must
    reproduce, per week, exactly what the retired week_start cube's own
    rows were (recomputed here directly from the same silver source the
    retired cube read, rather than compared against a table that no
    longer exists)."""

    def test_web_funnel_weekly_rollup_matches_direct_week_truncation(self, driver):
        rollup = driver.query(
            "SELECT DATE_TRUNC('week', day_date) AS week_start, "
            "SUM(test_starts) AS test_starts "
            "FROM gold.web_funnel_daily_cube GROUP BY 1 ORDER BY 1"
        ).reset_index(drop=True)
        direct = driver.query(
            "SELECT DATE_TRUNC('week', first_test_start_ts) AS week_start, "
            "COUNT(*) AS test_starts FROM silver.web_user_stages "
            "WHERE first_test_start_ts IS NOT NULL GROUP BY 1 ORDER BY 1"
        ).reset_index(drop=True)
        assert list(rollup["week_start"]) == list(direct["week_start"])
        assert list(rollup["test_starts"]) == list(direct["test_starts"])
        assert rollup["test_starts"].sum() == 100000

    def test_journey_weekly_rollup_matches_direct_week_truncation(self, driver):
        rollup = driver.query(
            "SELECT DATE_TRUNC('week', day_date) AS week_start, "
            "SUM(downloads) AS downloads "
            "FROM gold.journey_daily_cube GROUP BY 1 ORDER BY 1"
        ).reset_index(drop=True)
        direct = driver.query(
            "SELECT DATE_TRUNC('week', first_open_ts) AS week_start, "
            "COUNT(DISTINCT hashed_id) AS downloads FROM silver.linked_journeys "
            "GROUP BY 1 ORDER BY 1"
        ).reset_index(drop=True)
        assert list(rollup["week_start"]) == list(direct["week_start"])
        assert list(rollup["downloads"]) == list(direct["downloads"])
        assert rollup["downloads"].sum() == 6863


class TestCubesInGuardrailWhitelist:
    def test_both_cubes_in_gold_tables(self):
        from agent.medallion import GOLD_TABLES

        assert "web_funnel_daily_cube" in GOLD_TABLES
        assert "journey_daily_cube" in GOLD_TABLES

    def test_both_cubes_in_guardrail_allowlist(self):
        from agent.guardrails import ALLOWED_TABLES

        assert "gold.web_funnel_daily_cube" in ALLOWED_TABLES
        assert "gold.journey_daily_cube" in ALLOWED_TABLES

    def test_run_sql_can_query_the_cubes(self, driver):
        from agent.guardrails import enforce_limit, validate_sql

        sql = "SELECT COUNT(*) AS n FROM gold.web_funnel_daily_cube"
        validate_sql(sql)  # must not raise
        df = driver.query(enforce_limit(sql))
        assert df.iloc[0]["n"] > 0
