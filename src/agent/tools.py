"""Typed tool registry for the M3a agentic loop.

Every tool is a plain method returning a JSON-serializable result.
Failures raise :class:`ToolError` with a human-readable message (e.g. the
database's own error text) so :mod:`agent.agentic` can feed it back to
the LLM for self-correction instead of crashing the loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agent.db import BaseDriver
from agent.guardrails import ALLOWED_TABLES, GuardrailError, enforce_limit, validate_sql
from agent.knowledge import KnowledgeBase

#: Hard cap on rows returned to the LLM by run_sql / get_metric (keeps the
#: tool-result payload — and therefore the prompt — small).
MAX_RESULT_ROWS = 200

#: enforce_limit() budget for ad-hoc agentic SQL; smaller than the M1/M2
#: ask() path's 5000, since these rows round-trip through an LLM prompt.
MAX_SQL_ROWS = 200


class ToolError(Exception):
    """Raised by a tool when it cannot complete; message is shown to the LLM."""


@dataclass(frozen=True)
class ToolSpec:
    """One tool's provider-agnostic definition plus its Python handler."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema: {"type": "object", "properties": ..., "required": ...}
    handler: Callable[..., Any]


class ToolRegistry:
    """The tools available to the agentic loop for one running session.

    Bound to a single driver/metric-registry/knowledge-base triple so
    handlers can be plain bound methods.
    """

    def __init__(
        self,
        driver: BaseDriver,
        registry: dict[str, dict],
        knowledge: Optional[KnowledgeBase] = None,
    ) -> None:
        self.driver = driver
        self.metric_registry = registry
        self.knowledge = knowledge
        self._schema_cache: Optional[dict] = None
        self._specs: dict[str, ToolSpec] = {s.name: s for s in self._build_specs()}

    def _build_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="get_schema",
                description=(
                    "Return every queryable table (raw bronze events, silver "
                    "user/device-grain stages, gold KPI marts, plus the "
                    "legacy bare table names) with its columns, types and "
                    "3 sample rows. Call this before writing ad-hoc SQL if you "
                    "are unsure of exact column names or types."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.get_schema,
            ),
            ToolSpec(
                name="run_sql",
                description=(
                    "Run a single read-only SELECT/WITH statement against "
                    "web_events, app_events and id_bridge, and return up to "
                    f"{MAX_RESULT_ROWS} rows plus the total row_count. On "
                    "failure, raises an error containing the guardrail or "
                    "database message — read it, fix the query, and call "
                    "run_sql again."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "A single SELECT or WITH statement.",
                        }
                    },
                    "required": ["sql"],
                },
                handler=self.run_sql,
            ),
            ToolSpec(
                name="search_knowledge",
                description=(
                    "Search the analyst knowledge base (methodology, privacy/"
                    "consent, causal-interpretation insights, attribution "
                    "notes) for passages relevant to the query. Use this for "
                    "'why' / 'is this causal' / 'what does this caveat mean' "
                    "questions, and cite the returned source_file in your "
                    "final answer."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "k": {
                            "type": "integer",
                            "description": "How many passages to return (default 3).",
                        },
                    },
                    "required": ["query"],
                },
                handler=self.search_knowledge,
            ),
            ToolSpec(
                name="build_dashboard",
                description=(
                    "Build a filtered ~10-KPI dashboard over the dimensional "
                    "gold cubes (gold.web_funnel_daily_cube, "
                    "gold.journey_daily_cube -- DAY grain, so a day-level "
                    "filter is fully answerable) for a DASHBOARD / KPI-board / "
                    "overview request — NOT for a single-metric question "
                    "(use get_metric/run_sql for those instead). All "
                    "arguments are optional; omit one to leave that "
                    "dimension unfiltered. Prefer relative_range (e.g. "
                    "'last 3 days', 'last 3 months', 'last 6 weeks') for a "
                    "relative date ask — it is anchored to the LATEST date "
                    "actually in the data, never wall-clock today; only "
                    "days/weeks/months are supported (an unsupported unit, "
                    "e.g. 'hours', raises an error naming the supported "
                    "units instead of silently being dropped — call this "
                    "again with a supported phrase). date_start/date_end "
                    "(ISO 'YYYY-MM-DD') are used only when relative_range "
                    "is omitted. Returns dashboard cards plus a short "
                    "filter_label and a headline_summary you can build your "
                    "final answer from — your final answer to the user MUST "
                    "state the exact applied filter_label, and say so "
                    "explicitly if it differs from what was asked. On an "
                    "invalid market/channel/device/platform value the error "
                    "names the valid values for that field — call this "
                    "again with one of those."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "relative_range": {
                            "type": "string",
                            "description": "e.g. 'last 3 days', 'last 3 months', 'last 6 weeks'.",
                        },
                        "date_start": {
                            "type": "string",
                            "description": "ISO date YYYY-MM-DD; used only if relative_range is omitted.",
                        },
                        "date_end": {
                            "type": "string",
                            "description": "ISO date YYYY-MM-DD; used only if relative_range is omitted.",
                        },
                        "market": {"type": "string", "description": "e.g. 'DE', 'UK', 'US'."},
                        "channel": {"type": "string"},
                        "device": {"type": "string", "description": "'desktop', 'mobile' or 'tablet'."},
                        "platform": {"type": "string", "description": "'iOS' or 'Android'."},
                    },
                    "required": [],
                },
                handler=self.build_dashboard,
            ),
            ToolSpec(
                name="get_metric",
                description=(
                    "Fetch a registered KPI by key: runs its governed SQL and "
                    "returns the rows, chart spec and title. Prefer this over "
                    "run_sql whenever a registered KPI already answers the "
                    "question."
                ),
                parameters={
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
                handler=self.get_metric,
            ),
        ]

    @property
    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def as_tool_defs(self) -> list[dict]:
        """Provider-agnostic tool definitions: ``[{name, description, parameters}]``."""
        return [
            {"name": s.name, "description": s.description, "parameters": s.parameters}
            for s in self.specs
        ]

    def call(self, name: str, arguments: dict) -> Any:
        """Invoke a tool by name; raises :class:`ToolError` for an unknown name."""
        spec = self._specs.get(name)
        if spec is None:
            raise ToolError(
                f"Unknown tool '{name}'. Known tools: {', '.join(self._specs)}."
            )
        return spec.handler(**(arguments or {}))

    # ------------------------------------------------------------ handlers

    def get_schema(self) -> dict:
        """Table/column/type listing plus 3 sample rows per table (cached).

        Covers every table in :data:`agent.guardrails.ALLOWED_TABLES` — the
        legacy bare names plus the full bronze/silver/gold medallion layer
        — so the schema the agent sees always matches what run_sql/get_metric
        are actually allowed to query.
        """
        if self._schema_cache is not None:
            return self._schema_cache
        tables: dict[str, dict] = {}
        for table in sorted(ALLOWED_TABLES):
            described = self.driver.query(f"DESCRIBE {table}")
            columns = [
                {"name": str(row["column_name"]), "type": str(row["column_type"])}
                for _, row in described.iterrows()
            ]
            sample = self.driver.query(f"SELECT * FROM {table} LIMIT 3")
            tables[table] = {
                "columns": columns,
                "sample_rows": _rows_to_json(sample),
            }
        self._schema_cache = {"tables": tables}
        return self._schema_cache

    def run_sql(self, sql: str) -> dict:
        """Guardrail-checked, limit-enforced ad-hoc SQL. Rows capped at 200."""
        try:
            validate_sql(sql)
            guarded_sql = enforce_limit(sql, max_rows=MAX_SQL_ROWS)
            df = self.driver.query(guarded_sql)
        except GuardrailError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface the DB's own message
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc
        return {
            "sql": guarded_sql,
            "rows": _rows_to_json(df.head(MAX_RESULT_ROWS)),
            "row_count": len(df),
        }

    def build_dashboard(
        self,
        relative_range: Optional[str] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        market: Optional[str] = None,
        channel: Optional[str] = None,
        device: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> dict:
        """Module M11: filtered ~10-KPI dashboard over the two gold cubes.

        ``relative_range`` (free text like "last 3 months") wins over any
        explicit ``date_start``/``date_end`` when both are given, and is
        resolved against this driver's actual data horizon (never
        wall-clock today) — see ``agent.dashboard.get_data_horizon``.
        """
        from agent import dashboard as dash
        from pydantic import ValidationError

        range_label: Optional[str] = None
        if relative_range:
            horizon = dash.get_data_horizon(self.driver)
            # M11-fix "honesty rule": resolve_relative_range raises a
            # structured UnparseableRangeError (naming the supported units)
            # for a phrase like 'last 3 hours' instead of silently doing
            # nothing — surfaced to the LLM as a ToolError so it retries
            # with a supported unit rather than quietly substituting one
            # and never telling the user (the real-run bug this fixes).
            try:
                start_d, end_d, range_label = dash.resolve_relative_range(relative_range, horizon)
            except dash.UnparseableRangeError as exc:
                raise ToolError(str(exc)) from exc
            date_start, date_end = start_d.isoformat(), end_d.isoformat()
        try:
            filters = dash.DashboardFilters(
                date_start=date_start,
                date_end=date_end,
                market=market,
                channel=channel,
                device=device,
                platform=platform,
            )
        except ValidationError as exc:
            raise ToolError(f"Invalid filter value(s): {exc}") from exc
        try:
            return dash.run_dashboard(self.driver, filters, range_label=range_label)
        except dash.FilterValidationError as exc:
            raise ToolError(f"Invalid filter '{exc.field}': {exc}") from exc

    def search_knowledge(self, query: str, k: int = 3) -> dict:
        """Top-k knowledge-base passages for ``query`` (empty if no KB configured)."""
        if self.knowledge is None:
            return {"results": []}
        return {"results": self.knowledge.search(query, k=k)}

    def get_metric(self, key: str) -> dict:
        """A registered KPI's SQL, chart spec and rows, executed."""
        if key not in self.metric_registry:
            raise ToolError(
                f"Unknown metric '{key}'. Known: {', '.join(self.metric_registry)}."
            )
        metric = self.metric_registry[key]
        df = self.driver.query(metric["sql"])
        return {
            "key": key,
            "title": metric["title"],
            "sql": metric["sql"],
            "chart": metric.get("chart"),
            "rows": _rows_to_json(df.head(MAX_RESULT_ROWS)),
            "row_count": len(df),
        }


def _rows_to_json(df) -> list[dict]:
    """DataFrame -> JSON-safe list of dicts (NaN/Inf -> null, dates -> ISO)."""
    return json.loads(df.to_json(orient="records", date_format="iso"))
