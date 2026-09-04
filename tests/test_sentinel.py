"""Module M5: the sentinel — anomaly & schema-drift watchdog (PDF task 5.5).

Everything here runs fully offline and deterministically (see tests/conftest
for the AGENT_LLM=keyword hermetic guarantee, honored automatically by
agent.sentinel_core.narrate since KeywordLLM has no chat_step).

Two kinds of driver are used:

* the session-scoped `driver` fixture (tests/conftest.py) — a real
  DuckDBDriver over the actual data/ parquet files, used for the "healthy
  data" tests. Read-only: nothing here ever mutates it.
* `raw_con` (this file) — a FRESH in-memory DuckDB connection, per test,
  with its own real TABLE copies of the three parquet files (not views),
  so a test can freely DELETE/INSERT to build a tampered fixture without
  ever touching data/ on disk, and without other tests seeing the mutation.

No test hardcodes a calibrated funnel literal (e.g. the reference 42/55/61/78
percentages from src/generate_data.py) — every assertion below is either a
band-math boundary computed by hand, or a before/after comparison against
whatever the current data actually contains.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from agent import sentinel_core as sc
from scripts import sentinel as sentinel_cli

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


class QueryWrapper:
    """Minimal agent.sentinel_core.Queryable adapter around a bare DuckDB
    connection — the same `.query(sql) -> DataFrame` shape as
    agent.db.BaseDriver, without pulling in DuckDBDriver's parquet-view /
    medallion setup (sentinel only ever needs the three raw tables)."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con

    def query(self, sql: str) -> pd.DataFrame:
        return self._con.execute(sql).fetchdf()


@pytest.fixture
def raw_con() -> duckdb.DuckDBPyConnection:
    """A fresh in-memory DuckDB connection with real, mutable TABLE copies
    of the three raw parquet files — never the data/ files themselves."""
    con = duckdb.connect(":memory:")
    for name in sc.RAW_TABLES:
        path = (DATA_DIR / f"{name}.parquet").as_posix()
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM read_parquet('{path}')")
    return con


@pytest.fixture(scope="module")
def sentinel_registry() -> dict:
    """The shipped config/sentinel_registry.json, generated from this same
    current data (see agent.sentinel_core.build_registry)."""
    return sc.load_registry()


# ---------------------------------------------------------------------------
# Healthy data: as-of = max date -> no critical findings.
# ---------------------------------------------------------------------------


class TestHealthyRun:
    def test_default_as_of_has_no_critical_findings(self, driver, sentinel_registry):
        as_of = sc.default_as_of(driver)
        run = sc.run_checks(driver, as_of, sentinel_registry)
        critical = [f for f in run.findings if f.severity == "critical"]
        assert critical == [], f"unexpected critical finding(s): {critical}"
        assert run.exit_code in (0, 1)

    def test_default_as_of_has_no_schema_drift(self, driver, sentinel_registry):
        as_of = sc.default_as_of(driver)
        run = sc.run_checks(driver, as_of, sentinel_registry)
        schema_findings = [
            f
            for f in run.findings
            if f.check
            in ("schema_columns", "schema_event_names", "schema_app_versions", "missing_event_today")
        ]
        assert schema_findings == []

    def test_maturity_buffer_is_documented_and_positive(self):
        # A sanity check on the constant itself, not on any data value.
        assert sc.EVENT_MATURITY_BUFFER_DAYS > 0

    def test_scoring_the_raw_max_event_date_is_intentionally_unsafe(
        self, driver, sentinel_registry
    ):
        """Documents WHY default_as_of() subtracts a maturity buffer instead
        of literally using MAX(event_timestamp): in this fixed, closed
        dataset the last calendar day is a partial day by construction (a
        live pipeline's "today" is always partial too — that's the whole
        reason production jobs score a matured day, not the still-
        accumulating one). Scoring it directly must trip at least one
        critical finding on data that is otherwise perfectly healthy —
        exactly the false alarm the buffer exists to prevent.
        """
        raw_max = driver.query(
            "SELECT MAX(d) AS max_day FROM ("
            "  SELECT CAST(MAX(event_timestamp) AS DATE) AS d FROM web_events"
            "  UNION ALL"
            "  SELECT CAST(MAX(event_timestamp) AS DATE) AS d FROM app_events"
            ") AS horizons"
        )
        raw_max_day = pd.Timestamp(raw_max["max_day"].iloc[0]).date().isoformat()
        run = sc.run_checks(driver, raw_max_day, sentinel_registry)
        critical = [f for f in run.findings if f.severity == "critical"]
        assert critical, "the immature tail day was expected to trip a critical finding"


# ---------------------------------------------------------------------------
# Tampered fixtures, built in-memory — data/ is never touched.
# ---------------------------------------------------------------------------


class TestTamperedVolumeAndRateDrop:
    """Scenario (a): delete ~60% of the as-of day's Android
    hearing_aid_paired app events -> both the volume check and the
    download_to_pair funnel-rate check must flag it."""

    def test_deleting_most_of_a_days_pairings_is_flagged_critical(
        self, driver, raw_con, sentinel_registry
    ):
        as_of = sc.default_as_of(driver)
        as_of_date = pd.Timestamp(as_of).date()

        app_events = raw_con.execute("SELECT * FROM app_events").fetchdf()
        target_mask = (
            (app_events["event_name"] == "hearing_aid_paired")
            & (app_events["platform"] == "Android")
            & (app_events["event_timestamp"].dt.date == as_of_date)
        )
        target_idx = app_events[target_mask].sort_values("hashed_device_id").index
        rows_before = len(target_idx)
        assert rows_before > 0, "fixture assumption broken: nothing to delete"

        # Deterministic 60% cut (no reliance on an unseeded random()).
        drop_idx = target_idx[: round(rows_before * 0.6)]
        tampered = app_events.drop(index=drop_idx)
        raw_con.execute("DELETE FROM app_events")
        raw_con.execute("INSERT INTO app_events SELECT * FROM tampered")

        rows_after = int(
            (
                (tampered["event_name"] == "hearing_aid_paired")
                & (tampered["platform"] == "Android")
                & (tampered["event_timestamp"].dt.date == as_of_date)
            ).sum()
        )
        assert rows_after < rows_before  # sanity: the tamper actually removed rows

        run = sc.run_checks(QueryWrapper(raw_con), as_of, sentinel_registry)

        volume_hits = [
            f
            for f in run.findings
            if f.check == "daily_event_volume"
            and f.subject == "app:hearing_aid_paired:Android"
        ]
        assert volume_hits, "expected the Android hearing_aid_paired volume drop to be caught"
        assert volume_hits[0].severity == "critical"
        assert volume_hits[0].details["actual_count"] == rows_after

        rate_hits = [f for f in run.findings if f.subject == "rate:download_to_pair"]
        assert rate_hits, "expected the download_to_pair rate drop to be caught"
        assert rate_hits[0].severity == "critical"

        assert run.exit_code == 2


class TestTamperedSchemaDrift:
    """Scenario (b): a rogue event_name plus a brand-new app_version ->
    both must surface as schema-drift findings."""

    def test_rogue_event_name_and_new_app_version_are_flagged(
        self, driver, raw_con, sentinel_registry
    ):
        as_of = sc.default_as_of(driver)
        raw_con.execute(
            """
            INSERT INTO app_events
                (hashed_device_id, platform, event_name, event_timestamp, app_version)
            VALUES
                ('rogue-device-0001', 'Android', 'device_paired',
                 CAST(? AS TIMESTAMP), '7.0.0-beta')
            """,
            [f"{as_of} 10:00:00"],
        )

        run = sc.run_checks(QueryWrapper(raw_con), as_of, sentinel_registry)

        event_name_hits = [
            f
            for f in run.findings
            if f.check == "schema_event_names" and "device_paired" in f.subject
        ]
        assert event_name_hits, "expected the rogue event_name to be flagged"
        assert event_name_hits[0].severity == "warning"
        assert event_name_hits[0].details["kind"] == "new"

        app_version_hits = [
            f
            for f in run.findings
            if f.check == "schema_app_versions" and "7.0.0-beta" in f.subject
        ]
        assert app_version_hits, "expected the new app_version to be flagged"
        assert app_version_hits[0].severity == "warning"

        assert run.exit_code >= 1


class TestTamperedMissingEventToday:
    """Scenario (c): an expected event has zero rows on the as-of day ->
    the registry-driven presence check must catch what the pure band check
    structurally cannot (a vanished series has no row to compute a z-score
    from in the first place)."""

    def test_expected_event_with_zero_rows_today_is_flagged_critical(
        self, driver, raw_con, sentinel_registry
    ):
        as_of = sc.default_as_of(driver)

        before = raw_con.execute(
            "SELECT COUNT(*) FROM app_events "
            "WHERE event_name = 'remote_support_session' "
            f"AND CAST(event_timestamp AS DATE) = DATE '{as_of}'"
        ).fetchone()[0]
        assert before > 0, "fixture assumption broken: nothing to delete"

        raw_con.execute(
            "DELETE FROM app_events "
            "WHERE event_name = 'remote_support_session' "
            f"AND CAST(event_timestamp AS DATE) = DATE '{as_of}'"
        )

        run = sc.run_checks(QueryWrapper(raw_con), as_of, sentinel_registry)

        missing_hits = [
            f
            for f in run.findings
            if f.check == "missing_event_today"
            and "remote_support_session" in f.subject
        ]
        assert missing_hits, "expected the zero-row expected event to be flagged"
        assert missing_hits[0].severity == "critical"
        assert missing_hits[0].details["as_of"] == as_of

        assert run.exit_code == 2


# ---------------------------------------------------------------------------
# Band math unit tests — small, hand-computed series, no driver at all.
# ---------------------------------------------------------------------------


class TestBandMath:
    def test_severity_tiers_at_exact_boundaries(self):
        t = dict(sc.DEFAULT_THRESHOLDS)
        big_volume, full_history = 100.0, t["min_history_days"]
        assert sc._band_severity(1.5, big_volume, full_history, t) == "info"
        assert sc._band_severity(-1.5, big_volume, full_history, t) == "info"
        assert sc._band_severity(2.5, big_volume, full_history, t) == "warning"
        assert sc._band_severity(-2.5, big_volume, full_history, t) == "warning"
        assert sc._band_severity(4.0, big_volume, full_history, t) == "critical"
        assert sc._band_severity(-4.0, big_volume, full_history, t) == "critical"

    def test_just_under_info_threshold_is_no_finding(self):
        t = dict(sc.DEFAULT_THRESHOLDS)
        assert sc._band_severity(1.49, 100.0, t["min_history_days"], t) is None

    def test_undefined_deviation_is_no_finding(self):
        t = dict(sc.DEFAULT_THRESHOLDS)
        assert sc._band_severity(None, 100.0, t["min_history_days"], t) is None
        assert sc._band_severity(float("nan"), 100.0, t["min_history_days"], t) is None

    def test_min_volume_floor_suppresses_a_tiny_noisy_series(self):
        t = dict(sc.DEFAULT_THRESHOLDS)
        # A huge z-score on a series far below the volume floor: suppressed.
        assert sc._band_severity(10.0, t["min_volume_floor"] - 1, t["min_history_days"], t) is None
        # The same z-score once the series clears the floor: not suppressed.
        assert sc._band_severity(10.0, t["min_volume_floor"], t["min_history_days"], t) == "critical"

    def test_min_history_guard_suppresses_a_too_young_series(self):
        t = dict(sc.DEFAULT_THRESHOLDS)
        assert sc._band_severity(10.0, 100.0, t["min_history_days"] - 1, t) is None
        assert sc._band_severity(10.0, 100.0, t["min_history_days"], t) == "critical"

    def test_evaluate_volume_findings_on_a_hand_built_series(self):
        thresholds = dict(sc.DEFAULT_THRESHOLDS)
        rows = [
            {
                "day": pd.Timestamp("2026-01-10"),
                "event_name": "widget_event",
                "segment": "US",
                "source": "web",
                "actual_count": 40,
                "band_avg": 100.0,
                "band_stddev": 10.0,
                "band_days": 28,
                "band_lower": 90.0,
                "band_upper": 110.0,
                "deviation_z": (40 - 100.0) / 10.0,  # -6.0 -> critical
            },
            {
                "day": pd.Timestamp("2026-01-10"),
                "event_name": "steady_event",
                "segment": "US",
                "source": "web",
                "actual_count": 101,
                "band_avg": 100.0,
                "band_stddev": 10.0,
                "band_days": 28,
                "band_lower": 90.0,
                "band_upper": 110.0,
                "deviation_z": (101 - 100.0) / 10.0,  # +0.1 -> inside band
            },
        ]
        findings = sc.evaluate_volume_findings(pd.DataFrame(rows), thresholds)
        assert len(findings) == 1
        assert findings[0].subject == "web:widget_event:US"
        assert findings[0].severity == "critical"
        assert findings[0].details["deviation_z"] == pytest.approx(-6.0)

    def test_worst_severity_and_exit_code_mapping(self):
        info = sc.Finding("c", "info", "s1", "m1")
        warning = sc.Finding("c", "warning", "s2", "m2")
        critical = sc.Finding("c", "critical", "s3", "m3")
        assert sc.worst_severity([]) is None
        assert sc.exit_code([]) == 0
        assert sc.worst_severity([info]) == "info"
        assert sc.exit_code([info]) == 0
        assert sc.worst_severity([info, warning]) == "warning"
        assert sc.exit_code([info, warning]) == 1
        assert sc.worst_severity([info, warning, critical]) == "critical"
        assert sc.exit_code([warning, critical]) == 2


# ---------------------------------------------------------------------------
# Narration: statistics detect, KeywordLLM-backed narration never invents.
# ---------------------------------------------------------------------------


class TestNarration:
    def test_clean_run_template_summary_is_short_and_says_no_action(self):
        text = sc.template_summary([], "2026-08-24")
        assert "2026-08-24" in text
        assert "No action" in text

    def test_findings_template_summary_uses_only_given_numbers(self):
        findings = [
            sc.Finding(
                "daily_event_volume",
                "critical",
                "app:hearing_aid_paired:Android",
                "hearing_aid_paired (app / Android) on 2026-08-24: actual=30 is 6.21σ "
                "below the trailing 28-day band (77.9 ± 7.7).",
            )
        ]
        text = sc.template_summary(findings, "2026-08-24")
        assert "1 finding" in text
        assert "critical" in text
        assert "hearing_aid_paired" in text

    def test_narrate_without_llm_falls_back_to_template(self):
        text = sc.narrate([], "2026-08-24", llm=None)
        assert text == sc.template_summary([], "2026-08-24")

    def test_narrate_with_keyword_llm_falls_back_to_template(self):
        # KeywordLLM implements plan() only, not chat_step() -- exactly the
        # signal narrate() uses to know no real LLM is available (see
        # agent.llm's module docstring).
        from agent.llm import KeywordLLM

        text = sc.narrate([], "2026-08-24", llm=KeywordLLM())
        assert text == sc.template_summary([], "2026-08-24")

    def test_narrate_never_raises_when_chat_step_errors(self):
        class BrokenLLM:
            def chat_step(self, messages, tools):
                raise RuntimeError("no network in this sandbox")

        text = sc.narrate([], "2026-08-24", llm=BrokenLLM())
        assert text == sc.template_summary([], "2026-08-24")

    def test_narrate_uses_a_real_chat_step_reply_verbatim(self):
        class StubLLM:
            def chat_step(self, messages, tools):
                return {"content": "Everything looks fine today.", "tool_calls": []}

        text = sc.narrate([], "2026-08-24", llm=StubLLM())
        assert text == "Everything looks fine today."


# ---------------------------------------------------------------------------
# CLI (scripts/sentinel.py): report file, DRAFT header, exit codes, --notify.
# ---------------------------------------------------------------------------


class TestCLI:
    def test_healthy_run_writes_draft_report_and_exits_clean(self, driver, capsys):
        as_of = sc.default_as_of(driver)
        code = sentinel_cli.main(["--as-of", as_of, "--driver", "duckdb"])
        assert code == 0

        report_path = sentinel_cli.REPORTS_DIR / f"sentinel_{as_of}.md"
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert sentinel_cli.DRAFT_HEADER in content
        assert "pending analyst approval" in content
        assert f"# Sentinel Report — {as_of}" in content

        printed = capsys.readouterr().out
        assert sentinel_cli.DRAFT_HEADER in printed

    def test_json_format_prints_valid_json_with_draft_flag(self, driver, capsys):
        import json

        as_of = sc.default_as_of(driver)
        code = sentinel_cli.main(["--as-of", as_of, "--driver", "duckdb", "--format", "json"])
        assert code == 0
        printed = capsys.readouterr().out
        json_text = printed.split("\n[sentinel]")[0]
        payload = json.loads(json_text)
        assert payload["draft"] is True
        assert payload["as_of"] == as_of
        assert payload["human_checkpoint"] == "pending analyst approval"

    def test_notify_prints_instead_of_sending(self, driver, capsys):
        as_of = sc.default_as_of(driver)
        code = sentinel_cli.main(["--as-of", as_of, "--driver", "duckdb", "--notify"])
        assert code == 0
        printed = capsys.readouterr().out
        assert "[NOTIFY — NOT SENT]" in printed
        assert "Would post to Slack channel" in printed
        assert sentinel_cli.NOTIFY_CHANNEL in printed
        # The design comment explaining the checkpoint must be right there
        # in the printed text, not just in source code.
        assert "human-checkpoint design" in printed or "approval gate" in printed

    def test_exit_code_reflects_worst_finding_severity(self, monkeypatch, driver):
        """Isolates the CLI's exit-code plumbing from real data by stubbing
        run_checks -- run_checks itself is already covered end-to-end by the
        tampered-fixture tests above."""

        def fake_run_checks(drv, as_of, registry):
            return sc.SentinelRun(
                as_of=as_of,
                findings=[sc.Finding("stub", "critical", "s", "stubbed critical finding")],
                volume_df=pd.DataFrame(columns=["event_name"]),
                rates_df=pd.DataFrame(columns=["step"]),
                columns_df=pd.DataFrame(columns=["table_name", "column_name", "data_type"]),
                event_names_df=pd.DataFrame(columns=["table_name", "event_name"]),
                app_versions_df=pd.DataFrame(columns=["app_version"]),
                registry=registry,
            )

        monkeypatch.setattr(sentinel_cli.sc, "run_checks", fake_run_checks)
        # A deliberately distinct as-of (not the real default_as_of()) so this
        # stubbed run writes its own report file rather than clobbering the
        # genuine healthy-run report other tests/deliverables rely on.
        stub_as_of = "1999-01-01"
        code = sentinel_cli.main(["--as-of", stub_as_of, "--driver", "duckdb"])
        assert code == 2
        stub_report = sentinel_cli.REPORTS_DIR / f"sentinel_{stub_as_of}.md"
        assert stub_report.exists()
        stub_report.unlink()  # tidy up: this file exists only to prove the plumbing works

    def test_never_writes_to_bronze_silver_gold_or_raw_tables(self, driver, sentinel_registry):
        """Sentinel is read-only end to end: running the full check set must
        not change row counts anywhere it touches."""
        before = {
            t: driver.query(f"SELECT COUNT(*) AS n FROM {t}")["n"].iloc[0]
            for t in ("web_events", "app_events", "id_bridge")
        }
        as_of = sc.default_as_of(driver)
        sc.run_checks(driver, as_of, sentinel_registry)
        after = {
            t: driver.query(f"SELECT COUNT(*) AS n FROM {t}")["n"].iloc[0]
            for t in ("web_events", "app_events", "id_bridge")
        }
        assert before == after
