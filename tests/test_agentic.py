"""Tests for the M3a agentic tool-use loop, driven entirely by ScriptedLLM.

No network call is ever made here: ScriptedLLM is a deterministic stand-in
for a real provider's chat_step(), so these tests prove the loop's control
flow (event ordering, SQL self-correction, retry exhaustion, the tool-call
cap) without any dependency on api.openai.com or api.anthropic.com.
"""

from __future__ import annotations

import pytest

from agent.agentic import MAX_SQL_RETRIES, MAX_TOOL_CALLS, AgenticFunnelAgent
from agent.knowledge import KnowledgeBase
from agent.llm import KeywordLLM
from agent.memory import ConversationMemory
from agent.testing import ScriptedLLM


@pytest.fixture(scope="module")
def knowledge():
    return KnowledgeBase()


@pytest.fixture
def make_agent(driver, knowledge):
    def _make(turns, memory=None):
        return AgenticFunnelAgent(
            driver=driver, llm_chat=ScriptedLLM(turns), knowledge=knowledge, memory=memory
        )

    return _make


def event_types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


class TestSupportsAgentic:
    def test_keyword_llm_does_not_support_agentic(self):
        assert AgenticFunnelAgent.supports_agentic(KeywordLLM()) is False

    def test_scripted_llm_supports_agentic(self):
        assert AgenticFunnelAgent.supports_agentic(ScriptedLLM([])) is True


class TestSystemPromptQualityRules:
    """Module M4a: three bug-fix quality steers added to the system prompt.

    String assertions only — the prompt is plain text fed to an LLM, there
    is nothing else to unit test here.
    """

    def test_warns_against_unnecessary_bridge_joins(self, make_agent):
        prompt = make_agent([])._system_prompt()
        assert "selection bias" in prompt
        assert "id_bridge" in prompt

    def test_prefers_layer_qualified_table_names(self, make_agent):
        prompt = make_agent([])._system_prompt()
        assert "layer-qualified" in prompt
        assert "bronze./silver./gold." in prompt

    def test_prefers_ctes_over_nested_subqueries(self, make_agent):
        prompt = make_agent([])._system_prompt()
        assert "CTEs" in prompt
        assert "nested subqueries" in prompt

    def test_qa5_requires_linkable_population_caveat(self, make_agent):
        """Module M7a QA-5: a one-line caveat is required whenever the
        answer relies on id_bridge/linked_journeys or a linkable-only
        gold mart — string-asserted, since the prompt is plain text fed
        to an LLM and there is nothing else to unit test here."""
        prompt = make_agent([])._system_prompt()
        assert "QA-5" in prompt
        assert "id_bridge" in prompt
        assert "linked_journeys" in prompt
        assert "consented, linkable population" in prompt


class TestHappyPath:
    def test_events_in_expected_order_and_final_answer(self, make_agent):
        turns = [
            {
                "tool_calls": [
                    {"id": "c1", "name": "search_knowledge", "arguments": {"query": "mobile completion"}}
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "c2",
                        "name": "run_sql",
                        "arguments": {
                            "sql": "SELECT device_category, count(*) AS n FROM web_events GROUP BY device_category"
                        },
                    }
                ]
            },
            {"content": "Mobile lags desktop; see insights.md for why.", "tool_calls": []},
        ]
        agent = make_agent(turns)
        events = list(agent.run("Why is mobile completion lower than desktop?"))

        types = event_types(events)
        assert types[0] == "plan"
        assert types[-1] == "answer"
        assert types == [
            "plan",
            "tool_call", "tool_result",
            "tool_call", "sql", "tool_result",
            "answer",
        ]

        answer_event = events[-1]
        assert answer_event["answer"] == "Mobile lags desktop; see insights.md for why."
        assert answer_event["sql"].startswith("SELECT device_category")
        assert answer_event["rows"]
        assert answer_event["citations"]
        assert all({"source_file", "heading"} <= set(c) for c in answer_event["citations"])
        assert answer_event["citations"][0] == {
            "source_file": "insights.md",
            "heading": "Why mobile completion is low: traffic quality vs. mobile UX",
        }

    def test_get_metric_emits_chart_event(self, make_agent):
        turns = [
            {"tool_calls": [{"id": "c1", "name": "get_metric", "arguments": {"key": "funnel_overview"}}]},
            {"content": "Here is the funnel overview.", "tool_calls": []},
        ]
        agent = make_agent(turns)
        events = list(agent.run("Show me the funnel"))
        chart_events = [e for e in events if e["type"] == "chart"]
        assert len(chart_events) == 1
        assert chart_events[0]["chart"]["type"] == "funnel"
        assert events[-1]["chart"]["type"] == "funnel"


class TestBuildDashboardM11:
    """Module M11: build_dashboard tool, exercised via
    agent.testing.dashboard_script — the same deterministic script a
    real tool-calling LLM's turns would produce for a dashboard question,
    without ever calling a real provider."""

    def test_dashboard_tool_call_emits_dashboard_event(self, make_agent):
        from agent.testing import dashboard_script

        question = "Build me a KPI dashboard for the last 3 months for Germany"
        agent = make_agent(dashboard_script(question))
        events = list(agent.run(question))

        types = [e["type"] for e in events]
        assert "dashboard" in types
        assert types[-1] == "answer"

        dash_events = [e for e in events if e["type"] == "dashboard"]
        assert len(dash_events) == 1
        assert dash_events[0]["filter_label"] == "Last 3 months · DE"
        assert len(dash_events[0]["cards"]) >= 10
        for card in dash_events[0]["cards"]:
            assert {"key", "title", "chart", "rows", "answer", "sql"} <= set(card)
            assert card["sql"], f"{card['key']} has no sql"
        # M11 addendum 2: call_id matches the build_dashboard tool_call's
        # own id, so the frontend can attach the per-KPI SQL list onto
        # that exact trace step.
        assert dash_events[0]["call_id"] == "c1"
        tool_call_events = [e for e in events if e["type"] == "tool_call" and e["name"] == "build_dashboard"]
        assert tool_call_events and tool_call_events[0]["call_id"] == dash_events[0]["call_id"]

    def test_system_prompt_carries_the_dashboard_intent_rule(self, make_agent):
        prompt = make_agent([])._system_prompt()
        assert "build_dashboard" in prompt
        assert "DASHBOARD INTENT" in prompt

    def test_system_prompt_carries_the_m11fix_honesty_rule(self, make_agent):
        """M11-fix: the real bug was never the parser alone -- a real LLM
        got a ToolError back for an unsupported unit and silently retried
        with a DIFFERENT range instead of telling the user. The fix that
        actually closes that gap is this instruction; assert it verbatim
        enough to catch it being edited away."""
        prompt = make_agent([])._system_prompt()
        assert "HONESTY RULE" in prompt
        assert "filter_label" in prompt
        assert "supported units" in prompt

    def test_invalid_filter_value_surfaces_as_a_failed_tool_result(self, make_agent):
        turns = [
            {
                "tool_calls": [
                    {"id": "c1", "name": "build_dashboard", "arguments": {"market": "Atlantis"}}
                ]
            },
            {"content": "Let me try DE instead.", "tool_calls": []},
        ]
        agent = make_agent(turns)
        events = list(agent.run("Build me a dashboard for Atlantis"))
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0]["ok"] is False
        assert "market" in tool_results[0]["result"]["error"]
        # A failed build_dashboard must never emit a "dashboard" event.
        assert all(e["type"] != "dashboard" for e in events)

    def test_unparseable_relative_range_surfaces_as_a_failed_tool_result(self, make_agent):
        """M11-fix: an unsupported unit (e.g. hours) must come back as a
        failed tool_result naming the supported units, giving the LLM a
        chance to retry correctly -- never a silently-substituted range."""
        turns = [
            {
                "tool_calls": [
                    {"id": "c1", "name": "build_dashboard", "arguments": {"relative_range": "last 3 hours"}}
                ]
            },
            {"content": "Let me try days instead.", "tool_calls": []},
        ]
        agent = make_agent(turns)
        events = list(agent.run("Build me a dashboard for the last 3 hours"))
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0]["ok"] is False
        message = tool_results[0]["result"]["error"]
        assert "days" in message and "weeks" in message and "months" in message
        assert all(e["type"] != "dashboard" for e in events)


class TestSelfCorrection:
    def test_broken_sql_then_corrected_sql_after_seeing_error(self, make_agent):
        def second_turn(messages, tools):
            # The most recent message must be the tool result carrying the
            # guardrail/DB error from the first (broken) run_sql attempt.
            last = messages[-1]
            assert last["role"] == "tool"
            assert "error" in last["content"].lower() or "not allowed" in last["content"].lower() \
                or "unknown" in last["content"].lower()
            return {
                "tool_calls": [
                    {"id": "c2", "name": "run_sql", "arguments": {"sql": "SELECT count(*) AS n FROM web_events"}}
                ]
            }

        turns = [
            {"tool_calls": [{"id": "c1", "name": "run_sql", "arguments": {"sql": "SELECT * FROM secret_table"}}]},
            second_turn,
            {"content": "There are several thousand web events.", "tool_calls": []},
        ]
        agent = make_agent(turns)
        events = list(agent.run("How many web events are there?"))

        types = event_types(events)
        # exact self-correction sequence: fail, retry notice, succeed, answer
        assert types == [
            "plan",
            "tool_call", "sql", "tool_result",
            "retry",
            "tool_call", "sql", "tool_result",
            "answer",
        ]

        first_result = events[3]
        assert first_result["type"] == "tool_result"
        assert first_result["ok"] is False

        retry_event = events[4]
        assert retry_event == {"type": "retry", "attempt": 1, "error": retry_event["error"]}
        assert retry_event["error"]

        second_result = events[7]
        assert second_result["type"] == "tool_result"
        assert second_result["ok"] is True

        answer_event = events[-1]
        assert answer_event["answer"] == "There are several thousand web events."
        # sql is pretty-printed for display (module M6): reindented and
        # keyword-uppercased, so the executed "... FROM web_events LIMIT
        # 200" now spans multiple lines rather than being one line.
        assert answer_event["sql"].startswith("SELECT count(*) AS n")
        assert "FROM web_events" in answer_event["sql"]
        assert "LIMIT 200" in answer_event["sql"]
        assert answer_event["rows"]

    def test_retry_exhaustion_yields_error_then_graceful_answer(self, make_agent):
        broken = {"tool_calls": [{"id": "cX", "name": "run_sql", "arguments": {"sql": "SELECT * FROM nope"}}]}
        turns = [broken, broken, broken]  # fails 3 times in a row: 1 initial + 2 retries
        agent = make_agent(turns)
        events = list(agent.run("Some unanswerable question"))

        types = event_types(events)
        assert types == [
            "plan",
            "tool_call", "sql", "tool_result", "retry",
            "tool_call", "sql", "tool_result", "retry",
            "tool_call", "sql", "tool_result",
            "error",
            "answer",
        ]
        retries = [e for e in events if e["type"] == "retry"]
        assert [r["attempt"] for r in retries] == [1, 2]
        assert len(retries) == MAX_SQL_RETRIES

        assert events[-2]["type"] == "error"
        answer_event = events[-1]
        assert answer_event["type"] == "answer"
        assert answer_event["answer"]  # a graceful apology, not empty
        assert "couldn't" in answer_event["answer"].lower() or "could not" in answer_event["answer"].lower() \
            or "try" in answer_event["answer"].lower()


class TestToolCallCap:
    def test_stops_after_max_tool_calls(self, make_agent):
        one_call_forever = {"tool_calls": [{"id": "cN", "name": "get_schema", "arguments": {}}]}
        # More scripted turns than the cap allows, to prove the loop itself
        # stops calling the LLM once the cap is hit rather than running out.
        turns = [one_call_forever] * (MAX_TOOL_CALLS + 3)
        agent = make_agent(turns)
        events = list(agent.run("Repeat forever"))

        tool_call_events = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_call_events) == MAX_TOOL_CALLS

        assert events[-2]["type"] == "error"
        assert "maximum number of tool calls" in events[-2]["message"]
        assert events[-1]["type"] == "answer"


class TestConversationMemoryIntegration:
    """Module M7a: partial conversation memory wired into the agentic loop.

    All ScriptedLLM-driven, per the offline-sandbox brief — no network,
    no real provider, fully deterministic.
    """

    def test_referential_question_with_history_injects_context_and_fires_event(self, make_agent):
        memory = ConversationMemory()
        memory.record(
            session_id="s1",
            question="Where is the biggest drop-off?",
            tables_used=["gold.step_conversion"],
            metric_key="step_conversion_rates",
            one_line_result="Highest drop-off: complete -> download at 32%.",
        )
        turns = [{"content": "It is higher on mobile too.", "tool_calls": []}]
        agent = make_agent(turns, memory=memory)
        events = list(agent.run("What about that on mobile?", session_id="s1"))

        assert [e["type"] for e in events] == ["plan", "context", "answer"]
        assert events[1] == {"type": "context", "text": "Using context from previous question"}

        system_message = agent.llm_chat.calls[0]["messages"][0]["content"]
        assert "PRIOR CONTEXT" in system_message
        assert "step_conversion_rates" in system_message
        assert "gold.step_conversion" in system_message

    def test_independent_long_question_is_never_contaminated_by_history(self, make_agent):
        """Contamination guard: unrelated, self-contained question must get
        NO context event and NO PRIOR CONTEXT block, even with a populated
        session history available."""
        memory = ConversationMemory()
        memory.record(session_id="s1", question="Where is the biggest drop-off?", one_line_result="x")
        turns = [{"content": "Pairing rate is highest on iOS.", "tool_calls": []}]
        agent = make_agent(turns, memory=memory)
        events = list(
            agent.run(
                "Compare hearing aid pairing rate by acquisition channel across markets",
                session_id="s1",
            )
        )

        assert [e["type"] for e in events] == ["plan", "answer"]
        system_message = agent.llm_chat.calls[0]["messages"][0]["content"]
        assert "PRIOR CONTEXT" not in system_message

    def test_memory_off_never_injects_even_when_referential_with_history(self, make_agent):
        memory = ConversationMemory()
        memory.record(session_id="s1", question="Where is the biggest drop-off?", one_line_result="x")
        turns = [{"content": "Sure, here it is again.", "tool_calls": []}]
        agent = make_agent(turns, memory=memory)
        events = list(
            agent.run("What about that again?", session_id="s1", memory_enabled=False)
        )

        assert "context" not in [e["type"] for e in events]
        system_message = agent.llm_chat.calls[0]["messages"][0]["content"]
        assert "PRIOR CONTEXT" not in system_message

    def test_no_session_id_never_injects(self, make_agent):
        memory = ConversationMemory()
        memory.record(session_id="s1", question="q", one_line_result="r")
        turns = [{"content": "Answer.", "tool_calls": []}]
        agent = make_agent(turns, memory=memory)
        events = list(agent.run("What about that?"))  # no session_id given
        assert "context" not in [e["type"] for e in events]

    def test_first_turn_ever_has_no_context_but_is_recorded_for_next_time(self, make_agent):
        memory = ConversationMemory()
        turns = [{"content": "First answer.", "tool_calls": []}]
        agent = make_agent(turns, memory=memory)
        events = list(agent.run("What about that?", session_id="s4"))
        assert "context" not in [e["type"] for e in events]  # nothing to inject yet
        assert memory.has_turns("s4") is True  # but it is recorded now

    def test_recording_happens_after_a_successful_answer(self, make_agent):
        memory = ConversationMemory()
        turns = [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "run_sql",
                        "arguments": {"sql": "SELECT count(*) AS n FROM web_events"},
                    }
                ]
            },
            {"content": "There are many web events.", "tool_calls": []},
        ]
        agent = make_agent(turns, memory=memory)
        list(agent.run("How many web events in total?", session_id="s2"))

        stored = memory.get_turns("s2")
        assert len(stored) == 1
        assert stored[0]["question"] == "How many web events in total?"
        assert stored[0]["tables_used"] == ["web_events"]
        assert stored[0]["one_line_result"] == "There are many web events."

    def test_recording_captures_metric_key_from_get_metric(self, make_agent):
        memory = ConversationMemory()
        turns = [
            {"tool_calls": [{"id": "c1", "name": "get_metric", "arguments": {"key": "funnel_overview"}}]},
            {"content": "Here is the funnel.", "tool_calls": []},
        ]
        agent = make_agent(turns, memory=memory)
        list(agent.run("Show me the funnel", session_id="s3"))

        stored = memory.get_turns("s3")
        assert stored[0]["metric_key"] == "funnel_overview"

    def test_no_memory_configured_is_a_complete_no_op(self, make_agent):
        # AgenticFunnelAgent(memory=None) — the default — must behave
        # exactly as before M7a even when session_id/memory_enabled are
        # passed to run().
        turns = [{"content": "Answer.", "tool_calls": []}]
        agent = make_agent(turns)  # no memory=...
        events = list(agent.run("What about that?", session_id="s5"))
        assert [e["type"] for e in events] == ["plan", "answer"]


class TestDefaultKnowledgeBase:
    def test_agent_builds_a_default_knowledge_base_when_none_given(self, driver):
        turns = [
            {"tool_calls": [{"id": "c1", "name": "search_knowledge", "arguments": {"query": "mobile completion"}}]},
            {"content": "See insights.md.", "tool_calls": []},
        ]
        agent = AgenticFunnelAgent(driver=driver, llm_chat=ScriptedLLM(turns), knowledge=None)
        events = list(agent.run("A question"))
        tool_result = next(e for e in events if e["type"] == "tool_result")
        assert tool_result["ok"] is True
        assert tool_result["result"]["results"]  # a default KnowledgeBase() was built and returned hits
