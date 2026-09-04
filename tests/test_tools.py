"""Unit tests for the M3a typed tool registry (agent.tools)."""

from __future__ import annotations

import pytest

from agent.guardrails import ALLOWED_TABLES
from agent.tools import ToolError, ToolRegistry


@pytest.fixture(scope="module")
def tools(driver, registry):
    return ToolRegistry(driver=driver, registry=registry, knowledge=None)


class TestGetSchema:
    def test_shape(self, tools):
        schema = tools.get_schema()
        # Since M3c, get_schema surfaces the full queryable surface: the
        # legacy bare names plus every qualified bronze/silver/gold object.
        assert set(schema["tables"]) == set(ALLOWED_TABLES)
        for table, info in schema["tables"].items():
            assert info["columns"], f"{table} has no columns"
            for col in info["columns"]:
                assert {"name", "type"} <= set(col)
            assert isinstance(info["sample_rows"], list)
            assert len(info["sample_rows"]) <= 3
            assert info["sample_rows"], f"{table} returned no sample rows"

    def test_is_cached(self, tools):
        first = tools.get_schema()
        second = tools.get_schema()
        assert first is second  # same object: computed once and cached

    def test_via_registry_call(self, tools):
        result = tools.call("get_schema", {})
        assert "tables" in result


class TestRunSql:
    def test_happy_path_returns_rows_and_row_count(self, tools):
        result = tools.run_sql("SELECT country, count(*) AS n FROM web_events GROUP BY country")
        assert result["rows"]
        assert result["row_count"] == len(result["rows"])
        assert "sql" in result

    def test_caps_rows_but_reports_true_row_count(self, tools):
        result = tools.run_sql("SELECT * FROM web_events")
        assert len(result["rows"]) <= 200
        assert result["row_count"] >= len(result["rows"])

    def test_guardrail_violation_raises_tool_error(self, tools):
        with pytest.raises(ToolError):
            tools.run_sql("SELECT * FROM secret_pii_table")

    def test_ddl_raises_tool_error(self, tools):
        with pytest.raises(ToolError):
            tools.run_sql("DROP TABLE web_events")

    def test_db_error_raises_tool_error_with_message(self, tools):
        # Passes guardrails (whitelisted table, SELECT-only) but is invalid
        # SQL a real database will reject — proves DB errors surface too.
        with pytest.raises(ToolError) as excinfo:
            tools.run_sql("SELECT nonexistent_column FROM web_events")
        assert str(excinfo.value)  # a non-empty, human-readable message

    def test_via_registry_call(self, tools):
        result = tools.call("run_sql", {"sql": "SELECT count(*) AS n FROM app_events"})
        assert result["rows"][0]["n"] > 0


class TestGetMetric:
    def test_known_key_returns_rows_and_chart(self, tools):
        result = tools.get_metric("funnel_overview")
        assert result["rows"]
        assert result["chart"]["type"] == "funnel"
        assert result["title"]
        assert result["sql"]

    def test_unknown_key_raises_tool_error(self, tools):
        with pytest.raises(ToolError):
            tools.get_metric("not_a_real_metric")

    def test_via_registry_call(self, tools):
        result = tools.call("get_metric", {"key": "weekly_test_starts_trend"})
        assert result["chart"]["type"] == "line"


class TestBuildDashboardTool:
    """Module M11 / M11-fix: the build_dashboard tool handler itself
    (agentic-loop event-shape tests live in test_agentic.py; this covers
    the handler's own relative_range resolution and error surfacing)."""

    def test_relative_range_days_resolves_and_narrows(self, tools):
        result = tools.build_dashboard(relative_range="last 3 days", market="DE")
        assert result["filter_label"] == "Last 3 days · DE"
        assert result["applied_range"]["label"] == "Last 3 days"

    def test_cards_carry_their_executed_sql(self, tools):
        # M11 addendum 2: the tool handler is a thin proxy over
        # agent.dashboard.run_dashboard(); assert its cards keep the "sql"
        # field all the way out to the agentic tool-call boundary.
        result = tools.build_dashboard(relative_range="last 3 days", market="DE")
        for card in result["cards"]:
            assert card["sql"], f"{card['key']} has no sql"
            assert "DE" in card["sql"]

    def test_unparseable_relative_range_raises_tool_error_naming_units(self, tools):
        """M11-fix honesty rule: an unsupported unit (e.g. hours) must be a
        structured, retryable ToolError -- not a silently-ignored filter
        that leaves the LLM free to quietly substitute a different range
        (the real-run bug this module fixes)."""
        with pytest.raises(ToolError) as exc_info:
            tools.build_dashboard(relative_range="last 3 hours")
        message = str(exc_info.value)
        assert "days" in message and "weeks" in message and "months" in message

    def test_explicit_dates_still_work_without_relative_range(self, tools):
        result = tools.build_dashboard(date_start="2026-08-27", date_end="2026-08-30")
        assert result["applied_range"] == {
            "start": "2026-08-27",
            "end": "2026-08-30",
            "label": None,
        }


class TestSearchKnowledgeWithoutKnowledgeBase:
    def test_returns_empty_results_when_no_kb_configured(self, tools):
        result = tools.search_knowledge("anything")
        assert result == {"results": []}


class TestToolDefsAndUnknownTool:
    def test_as_tool_defs_lists_all_five_tools(self, tools):
        names = {d["name"] for d in tools.as_tool_defs()}
        assert names == {
            "get_schema",
            "run_sql",
            "search_knowledge",
            "get_metric",
            "build_dashboard",  # module M11
        }
        for tool_def in tools.as_tool_defs():
            assert {"name", "description", "parameters"} <= set(tool_def)

    def test_unknown_tool_name_raises_tool_error(self, tools):
        with pytest.raises(ToolError):
            tools.call("not_a_real_tool", {})
