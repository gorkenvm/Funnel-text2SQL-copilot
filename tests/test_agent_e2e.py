"""End-to-end: the eight demo questions through FunnelAgent + KeywordLLM."""

from __future__ import annotations

import pytest

from agent.agent import FunnelAgent
from agent.demo import DEMO_QUESTIONS
from agent.llm import KeywordLLM


@pytest.fixture(scope="module")
def agent(driver):
    return FunnelAgent(driver=driver, llm=KeywordLLM())


@pytest.mark.parametrize("question", DEMO_QUESTIONS)
def test_demo_question_answers(agent, question):
    result = agent.ask(question)
    assert result["mode"] not in {"clarify", "refused", "error"}, (
        f"{question!r} -> {result['mode']}: {result['answer']}"
    )
    assert result["rows"], f"{question!r} returned no rows"
    assert result["answer"] and isinstance(result["answer"], str)
    assert result["sql"]


def test_rows_are_capped_for_display(agent):
    result = agent.ask("Weekly trend of test starts")
    assert len(result["rows"]) <= 50


def test_list_metrics(agent):
    metrics = agent.list_metrics()
    assert len(metrics) == 12
    assert all({"key", "title", "description"} <= set(m) for m in metrics)


def test_run_metric_direct(agent):
    result = agent.run_metric("funnel_overview")
    assert result["rows"]
    assert result["chart"]["type"] == "funnel"
    with pytest.raises(KeyError):
        agent.run_metric("nonexistent_metric")


def test_unmappable_question_clarifies(agent):
    result = agent.ask("What is the meaning of life?")
    assert result["mode"] == "clarify"
    assert result["rows"] == []


class TestKeywordLlmDashboardIntentM11:
    """Module M11: FunnelAgent.ask() + KeywordLLM.plan()'s deterministic
    dashboard path, end to end (no driver/LLM mocking) — this is the
    "offline demo" path the M11 spec calls out explicitly."""

    def test_plan_returns_dashboard_mode_with_extracted_filters(self):
        plan = KeywordLLM().plan(
            "Build me a KPI dashboard for the last 3 months for Germany",
            schema_doc="",
            metric_keys=[],
        )
        assert plan["mode"] == "dashboard"
        assert plan["filters"]["relative_range_text"] == "last 3 months"
        assert plan["filters"]["market"] == "DE"

    def test_ask_builds_a_filtered_dashboard(self, agent):
        result = agent.ask("Build me a KPI dashboard for the last 3 months for Germany")
        assert result["mode"] == "dashboard"
        assert result["filter_label"] == "Last 3 months · DE"
        assert len(result["cards"]) >= 10
        assert "Last 3 months" in result["answer"]

    def test_ask_dashboard_without_filters_covers_all_data(self, agent):
        result = agent.ask("build me a kpi dashboard")
        assert result["mode"] == "dashboard"
        assert result["filter_label"] == "All data"

    def test_ask_dashboard_with_invalid_filter_degrades_to_error_not_crash(self, agent):
        # extract_filters_from_text only ever proposes values it recognizes
        # (DE/UK/US or known aliases), so this exercises the FunnelAgent's
        # own defensive handling of agent.dashboard.FilterValidationError
        # by constructing a plan dict directly rather than round-tripping
        # through a question no regex would ever misparse.
        plan = {"mode": "dashboard", "filters": {"market": "Atlantis"}}
        result = agent._ask_dashboard("Build a dashboard for Atlantis", plan, "en")
        assert result["mode"] == "error"
        assert "market" in result["answer"]

    def test_single_metric_question_is_not_treated_as_a_dashboard(self, agent):
        result = agent.ask("Which channel completes the hearing test best?")
        assert result["mode"] != "dashboard"
