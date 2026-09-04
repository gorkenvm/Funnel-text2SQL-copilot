"""AgenticFunnelAgent: a hand-written multi-step tool-use loop.

Unlike the single-shot :class:`agent.agent.FunnelAgent` (module M1/M2),
this agent lets the LLM call tools (schema introspection, knowledge
search, ad-hoc SQL, registered-metric lookup) across multiple turns,
self-correcting SQL errors by feeding the database's own error message
back to the model. :meth:`AgenticFunnelAgent.run` is a **generator** that
yields typed trace events as they happen, so a caller (the ``/api/ask/
stream`` SSE endpoint) can stream a live "thinking" trace to the user;
the final event is always an ``"answer"``.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterator, Optional

from agent.agent import SCHEMA_DOC, _load_registry
from agent.db import BaseDriver
from agent.guardrails import extract_referenced_tables
from agent.knowledge import KnowledgeBase
from agent.memory import ConversationMemory, format_context_block, is_referential_question
from agent.sqlfmt import format_sql_for_display
from agent.tools import ToolError, ToolRegistry

#: Hard limits (per the M3a acceptance criteria): at most 6 tool calls and
#: ~60 seconds of wall clock per question, and at most 2 SQL self-correction
#: retries before giving up gracefully.
MAX_TOOL_CALLS = 6
# Overridable because the realistic budget depends on the model tier and the
# warehouse: gpt-4o turns + Databricks round-trips need more headroom than
# gpt-4o-mini + local DuckDB (which is what the original 60s assumed).
MAX_WALL_CLOCK_SECONDS = float(os.environ.get("AGENT_TIME_BUDGET_SECONDS", "120"))
MAX_SQL_RETRIES = 2

_APOLOGY = (
    "I couldn't complete this one cleanly, so here is my best partial "
    "result — please try rephrasing or narrowing the question."
)


class AgenticFunnelAgent:
    """Multi-step ask-the-funnel agent with a visible tool-use trace.

    Parameters
    ----------
    driver:
        The data driver (DuckDB or Databricks) tools run against.
    llm_chat:
        Anything implementing ``chat_step(messages, tools) -> dict`` — a
        real provider (:class:`agent.llm.OpenAILLM` /
        :class:`agent.llm.AnthropicLLM`) or the deterministic
        :class:`agent.testing.ScriptedLLM` test double.
    knowledge:
        Optional :class:`agent.knowledge.KnowledgeBase`; a default one is
        built (from ``docs/knowledge/*.md``) if omitted.
    memory:
        Optional :class:`agent.memory.ConversationMemory` (module M7a).
        When given, :meth:`run` can inject a "PRIOR CONTEXT" block into
        the system prompt for a referential follow-up question (see
        ``session_id``/``memory_enabled`` there) and always records the
        turn after a successful answer. ``None`` (the default) disables
        the feature entirely — :meth:`run` behaves exactly as before M7a.
    """

    def __init__(
        self,
        driver: BaseDriver,
        llm_chat: Any,
        knowledge: Optional[KnowledgeBase] = None,
        memory: Optional[ConversationMemory] = None,
    ) -> None:
        self.driver = driver
        self.llm_chat = llm_chat
        self.knowledge = knowledge if knowledge is not None else KnowledgeBase()
        self.memory = memory
        self.metric_registry = _load_registry()
        self.tools = ToolRegistry(
            driver=driver, registry=self.metric_registry, knowledge=self.knowledge
        )

    @staticmethod
    def supports_agentic(llm: Any) -> bool:
        """True when ``llm`` exposes the ``chat_step`` protocol this loop needs.

        :class:`agent.llm.KeywordLLM` does not implement ``chat_step`` —
        agentic mode requires a real provider (or a
        :class:`agent.testing.ScriptedLLM` in tests).
        """
        return callable(getattr(llm, "chat_step", None))

    def _system_prompt(self, context_block: Optional[str] = None) -> str:
        prompt = (
            "You are the analytics copilot for a hearing-test funnel "
            "(web -> app). Answer the user's question by calling tools as "
            "needed, then reply with a final plain-text message (no further "
            "tool call) that a business stakeholder can read.\n\n"
            f"{SCHEMA_DOC}\n\n"
            "Registered KPI keys you may fetch with get_metric: "
            f"{', '.join(self.metric_registry.keys())}\n\n"
            "GUARDRAILS for any SQL you write with run_sql:\n"
            "- exactly one SELECT/WITH statement, read-only, no semicolons;\n"
            "- only the tables web_events, app_events, id_bridge, or the "
            "qualified bronze.*/silver.*/gold.* medallion tables listed in "
            "get_schema;\n"
            "- prefer gold marts for standard KPIs, silver for user-grain "
            "analysis, and bronze for raw event queries; row-level "
            "cross-device joins are only ever allowed via "
            "silver.v_attribution_eligible;\n"
            "- cross-device web->app joins MUST go through id_bridge "
            "(web_pseudo_id <-> app_device_id) or, in the medallion layer, "
            "silver.v_attribution_eligible — never join web and app "
            "events directly;\n"
            "- if run_sql returns an error, read the message, fix the "
            "query and call run_sql again — you have a limited number of "
            "retries before you must answer with whatever you have.\n\n"
            "SQL QUALITY rules:\n"
            "- join id_bridge (or silver.v_attribution_eligible) ONLY when "
            "the question genuinely requires linking web and app "
            "identities; an unnecessary bridge join silently restricts "
            "your result to the consented, signed-in subsample and "
            "introduces selection bias — a single-platform question (web "
            "only, or app only) must not touch the bridge at all;\n"
            "- prefer layer-qualified table names (bronze./silver./gold.) "
            "over bare legacy names (web_events, app_events, id_bridge) "
            "so it is clear which lakehouse layer a query reads from;\n"
            "- for multi-step SQL, prefer readable CTEs (WITH ... AS (...)) "
            "over nested subqueries — one named step per CTE, not a query "
            "buried inside another query's FROM clause.\n\n"
            "Call search_knowledge for 'why', causal-interpretation, "
            "privacy/consent or attribution-choice questions, and cite the "
            "source_file(s) you used in your final answer (e.g. 'see "
            "methodology.md'). Prefer get_metric when a registered KPI "
            "already answers the question; use get_schema before ad-hoc "
            "SQL if you are unsure of columns; keep run_sql aggregate, not "
            "a raw event dump. When ONE registered metric covers the whole "
            "question, fetch exactly that one — the registry includes "
            "cross-tab metrics (e.g. completion_by_channel_device, "
            "attribution_first_vs_last, pairing_rate_by_platform_market); "
            "NEVER stitch several simpler metrics together when a single "
            "cross-tab metric already answers it.\n\n"
            "FORMAT of your final answer: concise prose, 2-5 sentences, "
            "optionally followed by a short bullet list of at most 5 items. "
            "NEVER emit a markdown table and NEVER use heading syntax (#, "
            "##, ###) — the UI already renders the returned rows and chart "
            "as a table/chart of its own, so repeating the data as a "
            "markdown table only duplicates it and renders poorly. Bold "
            "(**like this**) is fine for 1-2 key terms, nothing more.\n\n"
            "DASHBOARD INTENT (module M11): when the user's question asks "
            "for a DASHBOARD, KPI board, or an overview of several KPIs at "
            "once for some filter (e.g. 'build me a KPI dashboard for the "
            "last 3 months for Germany'), call build_dashboard with "
            "whatever filters you can extract (relative_range and/or "
            "market/channel/device/platform) INSTEAD OF get_metric/"
            "run_sql — do not try to answer a dashboard request by "
            "stitching together several get_metric calls. A question "
            "about ONE specific metric or number (e.g. 'what is the "
            "completion rate for DE?') is NOT a dashboard request — answer "
            "those with get_metric/run_sql as usual. relative_range only "
            "understands days/weeks/months — if you ask for an unsupported "
            "unit (e.g. hours), build_dashboard raises an error naming the "
            "supported units; retry with one of those rather than guessing "
            "a different range and staying silent about the swap. After "
            "build_dashboard returns, write your final answer as one short "
            "paragraph summarizing the headline numbers under that filter "
            "(the tool's headline_summary field is a good starting point, "
            "but restate it in your own words) — the dashboard cards "
            "themselves are already rendered separately, so do not repeat "
            "every card's numbers. HONESTY RULE (M11-fix): your final "
            "answer MUST explicitly state the exact applied filter — copy "
            "the tool result's filter_label verbatim (e.g. 'Last 3 days · "
            "DE') — and if the applied filter differs in any way from what "
            "the user actually asked for (a different date range, a "
            "dropped filter, a substituted unit), say so explicitly and "
            "explain why, rather than presenting the substituted result as "
            "if it were exactly what was requested.\n\n"
            "QA-5 (consent/linkability caveat): whenever your answer is "
            "based on id_bridge, silver.v_attribution_eligible, "
            "silver.linked_journeys, or any gold mart whose consent_note "
            "flags it as covering only the linkable/bridge-linked "
            "population, add one short caveat line to your written answer "
            "noting that the figures cover the consented, linkable "
            "population only — never present a bridge-derived number as "
            "if it were the total, unrestricted population."
        )
        if context_block:
            prompt = f"{prompt}\n\n{context_block}"
        return prompt

    def run(
        self,
        question: str,
        lang: str = "en",
        session_id: Optional[str] = None,
        memory_enabled: bool = True,
    ) -> Iterator[dict]:
        """Answer ``question``, yielding trace events; the last is ``"answer"``.

        Event shapes (``type`` is always present):

        * ``{"type": "plan", "text": str}``
        * ``{"type": "context", "text": str}`` — module M7a; emitted right
          after "plan" ONLY when prior conversation context was actually
          injected into the system prompt for this question (see
          ``session_id``/``memory_enabled`` below).
        * ``{"type": "tool_call", "name": str, "arguments": dict, "call_id": str}``
        * ``{"type": "tool_result", "name": str, "call_id": str, "result": Any, "ok": bool}``
        * ``{"type": "sql", "sql": str}`` — emitted alongside a run_sql tool_call
        * ``{"type": "retry", "attempt": int, "error": str}`` — a run_sql failure being retried
        * ``{"type": "chart", "chart": dict | None}`` — emitted after a get_metric tool_result
        * ``{"type": "dashboard", "cards": list[dict], "filter_label": str,
          "applied_range": dict | None, "call_id": str}`` — module M11,
          emitted after a successful build_dashboard tool_result;
          "applied_range" (M11-fix) isolates the date window actually used
          ({"start", "end", "label"}) from the compound filter_label
          string; each card carries a "sql" field (M11 addendum 2, the
          exact statement executed for that KPI); "call_id" (M11 addendum
          2) matches the "tool_call"/"tool_result" pair's call_id, so the
          frontend can attach the per-KPI SQL list onto the existing
          build_dashboard trace step
        * ``{"type": "error", "message": str}`` — a recoverable failure; always followed by "answer"
        * ``{"type": "answer", "answer": str, "sql": str | None, "rows": list,
          "chart": dict | None, "citations": [{"source_file", "heading"}]}``

        ``lang`` is currently a hint only (kept for forward compatibility
        with the M1/M2 ``lang`` contract); the tool-use system prompt is
        English regardless, matching the "English code" project rule.

        ``session_id``/``memory_enabled`` (module M7a, both optional and
        backward-compatible — omitting them behaves exactly as before):
        when this agent was built with a :class:`~agent.memory.ConversationMemory`
        (``memory`` constructor arg) AND ``memory_enabled`` is true AND
        ``session_id`` names a session with stored turns AND ``question``
        looks referential (:func:`agent.memory.is_referential_question`),
        prior turns are rendered into a "PRIOR CONTEXT" system-prompt
        block and the "context" event fires. Regardless of any of that,
        once this question is answered successfully (the no-tool-calls
        completion path — not a timeout/error fallback), the turn is
        recorded into memory for next time, provided ``session_id`` was
        given (``memory_enabled=False`` only gates *injection*, not
        recording — see the module M7a design notes).
        """
        start = time.monotonic()

        context_turns: list[dict] = []
        use_context = False
        if self.memory is not None and memory_enabled and session_id:
            context_turns = self.memory.get_turns(session_id)
            if context_turns and is_referential_question(question):
                use_context = True

        system_prompt = self._system_prompt(
            format_context_block(context_turns) if use_context else None
        )
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ]
        tool_defs = self.tools.as_tool_defs()

        tool_call_count = 0
        sql_retry_count = 0
        last_sql: Optional[str] = None
        last_rows: list = []
        last_chart: Optional[dict] = None
        last_metric_key: Optional[str] = None
        citations: list[dict] = []

        yield {"type": "plan", "text": f"Planning how to answer: {question!r}"}
        if use_context:
            yield {"type": "context", "text": "Using context from previous question"}

        while True:
            if time.monotonic() - start > MAX_WALL_CLOCK_SECONDS:
                yield {
                    "type": "error",
                    "message": (
                        "The agent ran out of time answering this question "
                        f"(budget {MAX_WALL_CLOCK_SECONDS:.0f}s, used "
                        f"{time.monotonic() - start:.0f}s over "
                        f"{tool_call_count} tool call(s))."
                    ),
                }
                yield self._fallback_answer(last_sql, last_rows, last_chart, citations)
                return

            try:
                turn = self.llm_chat.chat_step(messages, tool_defs)
            except Exception as exc:  # noqa: BLE001 - never let a planner crash kill the stream
                yield {"type": "error", "message": f"The planner failed: {exc}"}
                yield self._fallback_answer(last_sql, last_rows, last_chart, citations)
                return

            tool_calls = list(turn.get("tool_calls") or [])
            messages.append(
                {
                    "role": "assistant",
                    "content": turn.get("content"),
                    "tool_calls": tool_calls,
                }
            )

            if not tool_calls:
                answer_text = (turn.get("content") or "").strip() or (
                    "I don't have a specific answer for that — could you rephrase?"
                )
                if self.memory is not None and session_id:
                    self.memory.record(
                        session_id=session_id,
                        question=question,
                        tables_used=extract_referenced_tables(last_sql),
                        metric_key=last_metric_key,
                        one_line_result=answer_text,
                    )
                yield {
                    "type": "answer",
                    "answer": answer_text,
                    # Display-only formatting, applied at the last moment
                    # before this event is serialized to the client.
                    "sql": format_sql_for_display(last_sql),
                    "rows": last_rows,
                    "chart": last_chart,
                    "citations": citations,
                }
                return

            for call in tool_calls:
                if tool_call_count >= MAX_TOOL_CALLS:
                    yield {
                        "type": "error",
                        "message": "Reached the maximum number of tool calls for this question.",
                    }
                    yield self._fallback_answer(last_sql, last_rows, last_chart, citations)
                    return
                tool_call_count += 1

                name = call["name"]
                arguments = call.get("arguments") or {}
                call_id = call.get("id") or f"call_{tool_call_count}"

                yield {"type": "tool_call", "name": name, "arguments": arguments, "call_id": call_id}
                if name == "run_sql":
                    # Display-only formatting: the tool itself still runs the
                    # LLM's original (unformatted) `arguments["sql"]` below.
                    yield {"type": "sql", "sql": format_sql_for_display(arguments.get("sql", ""))}

                tool_started = time.monotonic()
                try:
                    result = self.tools.call(name, arguments)
                    ok = True
                except ToolError as exc:
                    result = {"error": str(exc)}
                    ok = False
                except Exception as exc:  # noqa: BLE001 - a tool must never crash the loop
                    result = {"error": f"{type(exc).__name__}: {exc}"}
                    ok = False

                yield {
                    "type": "tool_result",
                    "name": name,
                    "call_id": call_id,
                    "result": result,
                    "ok": ok,
                    "secs": round(time.monotonic() - tool_started, 2),
                }

                if name == "run_sql":
                    if ok:
                        last_sql = result.get("sql")
                        last_rows = result.get("rows", [])
                        sql_retry_count = 0
                    else:
                        sql_retry_count += 1
                        if sql_retry_count > MAX_SQL_RETRIES:
                            yield {
                                "type": "error",
                                "message": (
                                    "The query could not be corrected after "
                                    f"{MAX_SQL_RETRIES} retries."
                                ),
                            }
                            yield self._fallback_answer(last_sql, last_rows, last_chart, citations)
                            return
                        yield {
                            "type": "retry",
                            "attempt": sql_retry_count,
                            "error": result.get("error", ""),
                        }
                elif name == "get_metric" and ok:
                    last_sql = result.get("sql")
                    last_rows = result.get("rows", [])
                    last_chart = result.get("chart")
                    last_metric_key = result.get("key")
                    yield {"type": "chart", "chart": last_chart}
                elif name == "build_dashboard" and ok:
                    # Module M11: rendered by the frontend immediately (not
                    # folded into the terminal "answer" event the way
                    # "chart" is) — the dashboard grid should populate as
                    # soon as it's built, not only once the model finishes
                    # writing its final paragraph.
                    yield {
                        "type": "dashboard",
                        "cards": result.get("cards", []),
                        "filter_label": result.get("filter_label"),
                        "applied_range": result.get("applied_range"),
                        # M11 addendum 2: lets the frontend attach the
                        # per-KPI SQL list onto this build_dashboard
                        # tool_call's own trace step.
                        "call_id": call_id,
                    }
                elif name == "search_knowledge" and ok:
                    for hit in result.get("results", []):
                        citations.append(
                            {"source_file": hit["source_file"], "heading": hit["heading"]}
                        )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": json.dumps(result, default=str),
                    }
                )

    @staticmethod
    def _fallback_answer(
        sql: Optional[str],
        rows: list,
        chart: Optional[dict],
        citations: list[dict],
    ) -> dict:
        return {
            "type": "answer",
            "answer": _APOLOGY,
            # Display-only formatting, applied at the last moment before
            # this event is serialized to the client.
            "sql": format_sql_for_display(sql),
            "rows": rows,
            "chart": chart,
            "citations": citations,
        }
