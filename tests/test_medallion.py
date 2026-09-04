"""Module M3c: bronze/silver/gold medallion layer built from
sql/medallion.sql via agent.medallion.apply_medallion.

The ``driver`` fixture (tests/conftest.py) already applies the medallion
layer at DuckDBDriver construction time, so every test here just queries
the resulting bronze/silver/gold tables directly.

KPI equivalence is the load-bearing property of this module: the 4 oracle
SQL statements below are byte-for-byte copies of the pre-M3c metrics.yaml
SQL (computed directly over the raw event tables), kept here purely as an
independent "old way" to compute the same numbers. If a gold mart's
result ever drifts from what the raw-event SQL says, these tests fail.

Module M4b note: _OLD_COMPLETION_BY_CHANNEL_SQL originally picked a user's
channel via COALESCE(MAX(utm_campaign), 'organic/none') -- a shortcut that
only worked because, pre-M4b, every user had exactly one utm value across
their whole journey (the data-realism flaw M4b fixes). Now that the
generator plants genuine multi-touch journeys (a completion session can
close on a different utm than the one that started it) and repeat test
starts, a plain MAX() of the utm string is no longer equivalent to "the
channel of the user's most recent session" -- it can pick the ALPHABETICALLY
larger of two real utm values instead of the chronologically LATER one.
The oracle below was updated to pick the utm from the user's most recent
web_events row by event_timestamp (still computed independently, straight
off bronze/raw web_events, never touching silver/gold) -- i.e. it now
actually implements the "last touch" the old shortcut only approximated.
This is the one deliberate SQL change in this test file for M4b; every
other oracle here is untouched because it doesn't depend on utm_campaign.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

pytestmark = pytest.mark.usefixtures("driver")

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

GOLD_MARTS = (
    "funnel_overview",
    "step_conversion",
    "completion_by_channel",
    "completion_by_device",
    "downloads_by_channel",
    "pairing_by_channel",
    "pairing_by_platform_market",
    "d30_by_channel",
    "weekly_test_starts",
    "linkable_share_by_market",
    "attribution_first_vs_last",
    "completion_by_channel_device",
)

SILVER_TABLES = (
    "web_user_stages",
    "app_user_stages",
    "v_attribution_eligible",
    "linked_journeys",
)

BRONZE_TABLES = ("web_events", "app_events", "id_bridge")


# ---------------------------------------------------------------------------
# Layer inventory / row counts
# ---------------------------------------------------------------------------
class TestLayerInventory:
    @pytest.mark.parametrize("table", BRONZE_TABLES)
    def test_bronze_table_matches_raw_row_count(self, driver, table):
        bronze_count = driver.query(f"SELECT COUNT(*) AS n FROM bronze.{table}").iloc[0]["n"]
        raw_count = driver.query(f"SELECT COUNT(*) AS n FROM {table}").iloc[0]["n"]
        assert bronze_count == raw_count
        assert bronze_count > 0

    @pytest.mark.parametrize("table", SILVER_TABLES)
    def test_silver_table_has_rows(self, driver, table):
        n = driver.query(f"SELECT COUNT(*) AS n FROM silver.{table}").iloc[0]["n"]
        assert n > 0

    @pytest.mark.parametrize("mart", GOLD_MARTS)
    def test_gold_mart_has_rows(self, driver, mart):
        n = driver.query(f"SELECT COUNT(*) AS n FROM gold.{mart}").iloc[0]["n"]
        assert n > 0, f"gold.{mart} returned no rows"

    def test_silver_web_user_stages_matches_distinct_web_users(self, driver):
        silver_n = driver.query(
            "SELECT COUNT(*) AS n FROM silver.web_user_stages"
        ).iloc[0]["n"]
        raw_n = driver.query(
            "SELECT COUNT(DISTINCT user_pseudo_id) AS n FROM web_events"
        ).iloc[0]["n"]
        assert silver_n == raw_n

    def test_silver_app_user_stages_matches_distinct_app_devices_with_open(self, driver):
        silver_n = driver.query(
            "SELECT COUNT(*) AS n FROM silver.app_user_stages"
        ).iloc[0]["n"]
        raw_n = driver.query(
            "SELECT COUNT(DISTINCT hashed_device_id) AS n FROM app_events "
            "WHERE event_name = 'app_open'"
        ).iloc[0]["n"]
        assert silver_n == raw_n

    def test_v_attribution_eligible_row_count_equals_opt_in_rows(self, driver):
        eligible_n = driver.query(
            "SELECT COUNT(*) AS n FROM silver.v_attribution_eligible"
        ).iloc[0]["n"]
        opt_in_n = driver.query(
            "SELECT COUNT(*) AS n FROM id_bridge WHERE opt_in_flag = true"
        ).iloc[0]["n"]
        assert eligible_n == opt_in_n
        assert eligible_n > 0


# ---------------------------------------------------------------------------
# apply_medallion applier: clean run, {{raw}} templating, non-fatal COMMENT
# ---------------------------------------------------------------------------
class TestApplyMedallion:
    def test_runs_clean_on_a_fresh_duckdb_connection(self):
        import duckdb

        from agent.medallion import apply_medallion

        con = duckdb.connect(":memory:")
        for name in ("web_events", "app_events", "id_bridge"):
            path = (DATA_DIR / f"{name}.parquet").as_posix()
            con.execute(
                f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{path}')"
            )
        statements = apply_medallion(con.execute, raw_schema="main")
        assert len(statements) > 0

        for mart in GOLD_MARTS:
            n = con.execute(f"SELECT COUNT(*) AS n FROM gold.{mart}").fetchone()[0]
            assert n > 0

    def test_raw_token_is_substituted_everywhere(self):
        from agent.medallion import render_statements

        statements = render_statements("some_custom_schema")
        joined = "\n".join(statements)
        assert "{{raw}}" not in joined
        assert "some_custom_schema.web_events" in joined

    def test_comment_on_failure_is_logged_and_non_fatal(self):
        from agent.medallion import apply_medallion

        calls: list[str] = []

        def fake_execute(stmt: str) -> None:
            calls.append(stmt)
            if stmt.strip().upper().startswith("COMMENT ON"):
                raise RuntimeError("this engine does not support COMMENT ON")

        # Must not raise, even though every COMMENT ON statement "fails".
        statements = apply_medallion(fake_execute, raw_schema="main")
        assert any(s.strip().upper().startswith("COMMENT ON") for s in statements)

    def test_non_comment_failure_is_fatal(self):
        from agent.medallion import apply_medallion

        def fake_execute(stmt: str) -> None:
            if stmt.strip().upper().startswith("CREATE SCHEMA"):
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            apply_medallion(fake_execute, raw_schema="main")


# ---------------------------------------------------------------------------
# KPI equivalence: gold-backed metrics.yaml SQL vs. the pre-M3c direct-SQL
# oracle, computed here from the raw event tables.
# ---------------------------------------------------------------------------
_OLD_FUNNEL_OVERVIEW_SQL = """
WITH web_start AS (
  SELECT COUNT(DISTINCT user_pseudo_id) AS users
  FROM web_events WHERE event_name = 'hearing_test_start'
),
web_complete AS (
  SELECT COUNT(DISTINCT user_pseudo_id) AS users
  FROM web_events WHERE event_name = 'hearing_test_complete'
),
app_download AS (
  SELECT COUNT(DISTINCT hashed_device_id) AS users
  FROM app_events WHERE event_name = 'app_open'
),
app_paired AS (
  SELECT COUNT(DISTINCT hashed_device_id) AS users
  FROM app_events WHERE event_name = 'hearing_aid_paired'
),
first_open AS (
  SELECT hashed_device_id, MIN(event_timestamp) AS first_open_ts
  FROM app_events WHERE event_name = 'app_open'
  GROUP BY hashed_device_id
),
horizon AS (
  SELECT MAX(event_timestamp) AS max_ts FROM app_events
),
d30_active AS (
  SELECT COUNT(DISTINCT f.hashed_device_id) AS users
  FROM first_open f
  CROSS JOIN horizon h
  JOIN app_events a
    ON a.hashed_device_id = f.hashed_device_id
   AND a.event_name = 'app_open'
   AND a.event_timestamp >= f.first_open_ts + INTERVAL 28 DAY
   AND a.event_timestamp <  f.first_open_ts + INTERVAL 35 DAY
  WHERE f.first_open_ts <= h.max_ts - INTERVAL 34 DAY
)
SELECT 1 AS stage_order, 'hearing_test_start' AS stage, users FROM web_start
UNION ALL
SELECT 2, 'hearing_test_complete', users FROM web_complete
UNION ALL
SELECT 3, 'app_download', users FROM app_download
UNION ALL
SELECT 4, 'hearing_aid_paired', users FROM app_paired
UNION ALL
SELECT 5, 'active_d30', users FROM d30_active
ORDER BY stage_order
"""

_NEW_FUNNEL_OVERVIEW_SQL = """
SELECT stage_order, stage, users
FROM gold.funnel_overview
ORDER BY stage_order
"""

_OLD_COMPLETION_BY_CHANNEL_SQL = """
WITH ranked AS (
  -- The user's most recent web_events row by event_timestamp -- an
  -- independent (bronze-only) re-derivation of "last touch", see the M4b
  -- note in this file's module docstring for why MAX(utm_campaign) alone
  -- stopped being equivalent to this once multi-touch journeys are real.
  SELECT
    user_pseudo_id,
    utm_campaign,
    ROW_NUMBER() OVER (
      PARTITION BY user_pseudo_id ORDER BY event_timestamp DESC
    ) AS rn
  FROM web_events
),
last_touch AS (
  SELECT user_pseudo_id, COALESCE(utm_campaign, 'organic/none') AS channel
  FROM ranked
  WHERE rn = 1
),
per_user AS (
  SELECT
    w.user_pseudo_id,
    lt.channel,
    MAX(CASE WHEN w.event_name = 'hearing_test_start' THEN 1 ELSE 0 END) AS started,
    MAX(CASE WHEN w.event_name = 'hearing_test_complete' THEN 1 ELSE 0 END) AS completed
  FROM web_events w
  JOIN last_touch lt ON lt.user_pseudo_id = w.user_pseudo_id
  GROUP BY w.user_pseudo_id, lt.channel
)
SELECT
  channel,
  SUM(started) AS test_starts,
  SUM(completed) AS test_completes,
  1.0 * SUM(completed) / NULLIF(SUM(started), 0) AS completion_rate
FROM per_user
WHERE started = 1
GROUP BY channel
ORDER BY completion_rate DESC
"""

_NEW_COMPLETION_BY_CHANNEL_SQL = """
SELECT channel, test_starts, test_completes, completion_rate
FROM gold.completion_by_channel
ORDER BY completion_rate DESC
"""

_OLD_PAIRING_BY_PLATFORM_MARKET_SQL = """
WITH opened AS (
  SELECT hashed_device_id, MIN(platform) AS platform
  FROM app_events
  WHERE event_name = 'app_open'
  GROUP BY hashed_device_id
),
paired AS (
  SELECT DISTINCT hashed_device_id
  FROM app_events
  WHERE event_name = 'hearing_aid_paired'
)
SELECT
  b.market,
  o.platform,
  COUNT(DISTINCT o.hashed_device_id) AS app_devices,
  COUNT(DISTINCT p.hashed_device_id) AS paired_devices,
  1.0 * COUNT(DISTINCT p.hashed_device_id)
    / NULLIF(COUNT(DISTINCT o.hashed_device_id), 0) AS pairing_rate
FROM opened o
JOIN id_bridge b ON b.app_device_id = o.hashed_device_id
LEFT JOIN paired p ON p.hashed_device_id = o.hashed_device_id
GROUP BY b.market, o.platform
ORDER BY b.market, o.platform
"""

_NEW_PAIRING_BY_PLATFORM_MARKET_SQL = """
SELECT market, platform, app_devices, paired_devices, pairing_rate
FROM gold.pairing_by_platform_market
ORDER BY market, platform
"""

_OLD_D30_BY_CHANNEL_SQL = """
WITH first_open AS (
  SELECT hashed_device_id, MIN(event_timestamp) AS first_open_ts
  FROM app_events
  WHERE event_name = 'app_open'
  GROUP BY hashed_device_id
),
horizon AS (
  SELECT MAX(event_timestamp) AS max_ts FROM app_events
),
eligible AS (
  SELECT f.hashed_device_id, f.first_open_ts
  FROM first_open f
  CROSS JOIN horizon h
  WHERE f.first_open_ts <= h.max_ts - INTERVAL 34 DAY
),
active AS (
  SELECT DISTINCT e.hashed_device_id
  FROM eligible e
  JOIN app_events a
    ON a.hashed_device_id = e.hashed_device_id
   AND a.event_name = 'app_open'
   AND a.event_timestamp >= e.first_open_ts + INTERVAL 28 DAY
   AND a.event_timestamp <  e.first_open_ts + INTERVAL 35 DAY
)
SELECT
  b.acquisition_channel,
  COUNT(DISTINCT e.hashed_device_id) AS eligible_users,
  COUNT(DISTINCT a.hashed_device_id) AS retained_users,
  1.0 * COUNT(DISTINCT a.hashed_device_id)
    / NULLIF(COUNT(DISTINCT e.hashed_device_id), 0) AS d30_retention_rate
FROM eligible e
JOIN id_bridge b ON b.app_device_id = e.hashed_device_id
LEFT JOIN active a ON a.hashed_device_id = e.hashed_device_id
GROUP BY b.acquisition_channel
ORDER BY d30_retention_rate DESC
"""

_NEW_D30_BY_CHANNEL_SQL = """
SELECT acquisition_channel, eligible_users, retained_users, d30_retention_rate
FROM gold.d30_by_channel
ORDER BY d30_retention_rate DESC
"""


class TestKpiEquivalence:
    """Prove the gold-backed metrics.yaml SQL returns EXACTLY what the old
    direct-SQL-over-raw-events version did — same rows, same values."""

    @pytest.mark.parametrize(
        "old_sql, new_sql",
        [
            (_OLD_FUNNEL_OVERVIEW_SQL, _NEW_FUNNEL_OVERVIEW_SQL),
            (_OLD_COMPLETION_BY_CHANNEL_SQL, _NEW_COMPLETION_BY_CHANNEL_SQL),
            (_OLD_PAIRING_BY_PLATFORM_MARKET_SQL, _NEW_PAIRING_BY_PLATFORM_MARKET_SQL),
            (_OLD_D30_BY_CHANNEL_SQL, _NEW_D30_BY_CHANNEL_SQL),
        ],
        ids=["funnel_overview", "completion_by_channel", "pairing_by_platform_market", "d30_by_channel"],
    )
    def test_gold_backed_sql_matches_old_direct_sql(self, driver, old_sql, new_sql):
        old_df = driver.query(old_sql).reset_index(drop=True)
        new_df = driver.query(new_sql).reset_index(drop=True)
        pd.testing.assert_frame_equal(old_df, new_df, check_dtype=False)

    def test_registry_sql_is_the_gold_backed_version(self, registry):
        """metrics.yaml itself (not just a copy in this test) must be the
        thin gold-mart select — guards against someone reverting metrics.yaml
        to the old heavy SQL without updating the medallion layer too."""
        for key in (
            "funnel_overview",
            "completion_rate_by_channel",
            "pairing_rate_by_platform_market",
            "d30_retention_by_channel",
        ):
            sql = registry[key]["sql"].lower()
            assert "gold." in sql, f"{key}'s SQL no longer reads from a gold mart"

    @pytest.mark.parametrize(
        "key, new_sql",
        [
            ("funnel_overview", _NEW_FUNNEL_OVERVIEW_SQL),
            ("completion_rate_by_channel", _NEW_COMPLETION_BY_CHANNEL_SQL),
            ("pairing_rate_by_platform_market", _NEW_PAIRING_BY_PLATFORM_MARKET_SQL),
            ("d30_retention_by_channel", _NEW_D30_BY_CHANNEL_SQL),
        ],
    )
    def test_registry_sql_matches_the_gold_oracle_used_above(self, driver, registry, key, new_sql):
        """The actual metrics.yaml SQL for these 4 keys returns the same
        rows as the equivalent gold-mart SQL used in the equivalence test
        above (sanity check that the two aren't accidentally different
        gold-reading queries that happen to both look plausible)."""
        registry_df = driver.query(registry[key]["sql"]).reset_index(drop=True)
        oracle_df = driver.query(new_sql).reset_index(drop=True)
        pd.testing.assert_frame_equal(registry_df, oracle_df, check_dtype=False)


# ---------------------------------------------------------------------------
# Module M4a — attribution first-vs-last mart and the H1/H2 falsification
# cross-tab (gold.completion_by_channel_device).
# ---------------------------------------------------------------------------
class TestAttributionFirstVsLast:
    def test_has_both_attribution_models(self, driver):
        models = set(
            driver.query(
                "SELECT DISTINCT attribution_model FROM gold.attribution_first_vs_last"
            )["attribution_model"]
        )
        assert models == {"first_touch", "last_touch"}

    def test_total_attributed_downloads_is_equal_across_models(self, driver):
        """The INVARIANT this mart exists to guarantee: the same linkable
        population is counted twice, so the grand total must match exactly
        regardless of which attribution model it is sliced by."""
        totals = driver.query(
            "SELECT attribution_model, SUM(attributed_downloads) AS total "
            "FROM gold.attribution_first_vs_last "
            "GROUP BY attribution_model"
        )
        assert len(totals) == 2
        distinct_totals = totals["total"].nunique()
        assert distinct_totals == 1, (
            f"first_touch and last_touch totals disagree: "
            f"{totals.to_dict('records')}"
        )

    def test_total_matches_the_linkable_journeys_population(self, driver):
        """Independent oracle: the shared total must equal the row count of
        the linkable population the mart is built from (silver.linked_journeys),
        not some other number."""
        mart_total = driver.query(
            "SELECT SUM(attributed_downloads) AS n FROM gold.attribution_first_vs_last "
            "WHERE attribution_model = 'first_touch'"
        ).iloc[0]["n"]
        population_n = driver.query(
            "SELECT COUNT(*) AS n FROM silver.linked_journeys"
        ).iloc[0]["n"]
        assert mart_total == population_n
        assert mart_total > 0

    def test_no_negative_or_null_counts(self, driver):
        df = driver.query("SELECT * FROM gold.attribution_first_vs_last")
        assert df["attributed_downloads"].notna().all()
        assert (df["attributed_downloads"] >= 0).all()


class TestCompletionByChannelDevice:
    def test_row_count_equals_distinct_channel_device_combinations_present(self, driver):
        mart_n = driver.query(
            "SELECT COUNT(*) AS n FROM gold.completion_by_channel_device"
        ).iloc[0]["n"]
        distinct_n = driver.query(
            "SELECT COUNT(*) AS n FROM ("
            "  SELECT DISTINCT channel, device_category "
            "  FROM gold.completion_by_channel_device"
            ") t"
        ).iloc[0]["n"]
        assert mart_n == distinct_n
        assert mart_n > 0

    def test_completion_rate_is_a_fraction(self, driver):
        df = driver.query(
            "SELECT completion_rate FROM gold.completion_by_channel_device"
        )
        assert df["completion_rate"].notna().all()
        assert df["completion_rate"].between(0, 1).all()

    def test_users_completed_never_exceeds_users_started(self, driver):
        df = driver.query(
            "SELECT users_started, users_completed FROM gold.completion_by_channel_device"
        )
        assert (df["users_completed"] <= df["users_started"]).all()

    def test_paid_social_meta_desktop_completes_better_than_mobile(self, driver):
        """The planted H1/H2 confound (paid-social skews mobile; mobile has
        a lower completion propensity regardless of channel) must be visible
        WITHIN a single channel on this grid — read entirely from the mart
        itself, no calibrated literals asserted here."""
        df = driver.query(
            "SELECT device_category, completion_rate "
            "FROM gold.completion_by_channel_device "
            "WHERE channel = 'paid_social_meta'"
        ).set_index("device_category")["completion_rate"]
        assert "desktop" in df.index and "mobile" in df.index
        assert df["desktop"] > df["mobile"]


class TestPlantedPatternsSurviveM4b:
    """Module M4b plants genuine multi-touch (switcher) journeys and repeat
    test starts. completion_by_channel is last-touch-grouped, so switchers
    legitimately shift its exact rates (a channel that closes journeys
    started elsewhere gains share; a discovery channel that hands off its
    closers loses some) — that shift is the point of M4b, not a bug. What
    must survive is the ORDERING each hypothesis depends on, so these
    assertions are all relative/structural, never calibrated literals."""

    def test_organic_completion_far_exceeds_tiktok(self, driver):
        df = driver.query(
            "SELECT channel, completion_rate FROM gold.completion_by_channel"
        ).set_index("channel")["completion_rate"]
        assert df["organic/none"] > df["tiktok_awareness"] * 1.5

    def test_ios_pairing_beats_android_within_every_market(self, driver):
        """Platform/market/paired are all fixed before M4b's generator
        additions run, so this ~9pt planted gap is untouched by switchers
        or repeat starts — checked here directly off the gold mart."""
        df = driver.query(
            "SELECT market, platform, pairing_rate "
            "FROM gold.pairing_by_platform_market"
        ).pivot(index="market", columns="platform", values="pairing_rate")
        assert (pivot_gap := df["iOS"] - df["Android"]).gt(0.03).all(), pivot_gap.to_dict()

    def test_tiktok_has_the_lowest_pairing_among_linked_channels(self, driver):
        df = driver.query(
            "SELECT acquisition_channel, pairing_rate FROM gold.pairing_by_channel"
        ).set_index("acquisition_channel")["pairing_rate"]
        assert df["paid_social_tiktok"] == df.min()

    def test_linkable_share_ordering_de_lt_uk_lt_us(self, driver):
        df = driver.query(
            "SELECT market, linkable_share FROM gold.linkable_share_by_market"
        ).set_index("market")["linkable_share"]
        assert df["DE"] < df["UK"] < df["US"]
