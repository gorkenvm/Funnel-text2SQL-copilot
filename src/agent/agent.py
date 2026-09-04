"""FunnelAgent: question in, governed answer out.

Flow: ``llm.plan()`` decides between a registered KPI, ad-hoc SQL (which
must pass the guardrails) or a clarification. Results come back as a
compact dict with rows, a declarative chart spec and a plain-English
answer sentence.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml

from agent.db import BaseDriver
from agent.guardrails import GuardrailError, enforce_limit, validate_sql
from agent.llm import LLMClient
from agent.sqlfmt import format_sql_for_display

_METRICS_PATH = Path(__file__).resolve().parent / "metrics.yaml"
_MAX_DISPLAY_ROWS = 50

SCHEMA_DOC = """\
Tables (the only ones you may query):

web_events(user_pseudo_id, session_id, event_name, event_timestamp,
           page_location, utm_campaign NULLABLE, device_category,
           country IN (DE, UK, US), consent_state IN (granted, denied))
  event_name IN (page_view, hearing_test_start, hearing_test_complete,
                 result_screen_view, app_store_redirect)

app_events(hashed_device_id, platform IN (iOS, Android), event_name,
           event_timestamp, app_version)
  event_name IN (app_open, hearing_aid_paired, remote_support_session)

id_bridge(hashed_id, market, opt_in_flag, acquisition_channel,
          web_pseudo_id, app_device_id, linked_at)
  Contains ONLY consented + signed-in users. It is the single lawful way
  to join web and app data (web_pseudo_id <-> app_device_id).

Funnel: hearing_test_start -> hearing_test_complete -> app download
(first app_open; linkable only via id_bridge) -> hearing_aid_paired ->
active at day 28-34 after first open (censoring-aware).
"""


def _load_registry(path: Path = _METRICS_PATH) -> dict[str, dict]:
    """Load metrics.yaml into an ordered {key: metric} mapping."""
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    registry: dict[str, dict] = {}
    for metric in raw["metrics"]:
        registry[metric["key"]] = metric
    return registry


def _fmt(value: Any) -> str:
    """Human-friendly formatting for answer sentences."""
    if isinstance(value, float):
        if math.isnan(value):
            return "n/a"
        if 0.0 <= value <= 1.0:
            return f"{value * 100:.1f}%"
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


class FunnelAgent:
    """Ask-the-funnel agent tying together driver, LLM planner and registry."""

    def __init__(self, driver: BaseDriver, llm: LLMClient) -> None:
        self.driver = driver
        self.llm = llm
        self.registry = _load_registry()

    # ------------------------------------------------------------------ API

    def list_metrics(self) -> list[dict]:
        """Return key/title/description for every registered KPI."""
        return [
            {
                "key": m["key"],
                "title": m["title"],
                "description": m["description"].strip(),
            }
            for m in self.registry.values()
        ]

    def run_metric(self, key: str) -> dict:
        """Execute a registered KPI by key and return the result payload."""
        if key not in self.registry:
            raise KeyError(
                f"Unknown metric '{key}'. Known: {', '.join(self.registry)}"
            )
        metric = self.registry[key]
        df = self.driver.query(metric["sql"])
        return self._payload(
            question=None,
            mode="metric",
            sql=metric["sql"],
            df=df,
            chart=metric.get("chart"),
            metric=metric,
        )

    def ask(self, question: str, narrate_language: str = "en") -> dict:
        """Answer a natural-language question about the funnel.

        ``narrate_language`` is an optional, backwards-compatible hint
        ("en" default). When set to "tr" and the configured LLM is the
        real :class:`~agent.llm.AnthropicLLM` (i.e. it exposes a
        ``translate`` method), the final ``answer`` sentence is
        best-effort translated into Turkish. With the deterministic
        :class:`~agent.llm.KeywordLLM` (no API key configured) the
        answer always stays in English, regardless of this hint.
        """
        plan = self.llm.plan(
            question, SCHEMA_DOC, metric_keys=list(self.registry.keys())
        )
        mode = plan.get("mode", "clarify")
        hint = plan.get("narrative_hint")

        if mode == "dashboard":
            # M11: a dashboard/KPI-board request, not a single metric —
            # handled entirely separately (multiple SQLs, not one) and
            # returned immediately; see _ask_dashboard's docstring for why
            # this bypasses the single-`sql` execution path below.
            return self._ask_dashboard(question, plan, narrate_language)

        if mode == "metric" and plan.get("metric_key") in self.registry:
            metric = self.registry[plan["metric_key"]]
            sql = metric["sql"]
            chart = metric.get("chart")
        elif mode == "sql" and plan.get("sql"):
            metric = None
            sql = plan["sql"]
            chart = plan.get("chart")
        else:
            return {
                "question": question,
                "mode": "clarify",
                "sql": None,
                "rows": [],
                "chart": None,
                "answer": hint
                or "I need a bit more detail to answer that — could you "
                "rephrase or mention a KPI (funnel, completion, pairing, "
                "retention, downloads, trend)?",
            }

        try:
            if metric is None:  # ad-hoc SQL: guard first, always
                validate_sql(sql)
                sql = enforce_limit(sql)
            df = self.driver.query(sql)
        except GuardrailError as exc:
            return {
                "question": question,
                "mode": "refused",
                "sql": format_sql_for_display(sql),
                "rows": [],
                "chart": None,
                "answer": f"I can't run that query. {exc}",
            }
        except Exception as exc:  # noqa: BLE001 - surface DB errors politely
            return {
                "question": question,
                "mode": "error",
                "sql": format_sql_for_display(sql),
                "rows": [],
                "chart": None,
                "answer": "The query failed to execute "
                f"({type(exc).__name__}: {exc}). Try rephrasing the question.",
            }

        payload = self._payload(
            question=question,
            mode=mode,
            sql=sql,
            df=df,
            chart=chart,
            metric=metric,
            hint=hint,
        )
        translated = self._maybe_translate(payload["answer"], narrate_language)
        if translated:
            payload["answer"] = translated
        return payload

    def _ask_dashboard(self, question: str, plan: dict, narrate_language: str) -> dict:
        """Module M11: build a filtered ~10-KPI dashboard (see
        ``agent.dashboard``) for a dashboard/KPI-board question.

        Unlike the metric/sql branches above, this never runs a single
        ``self.driver.query(sql)`` — it runs one query per registered KPI
        template — so it returns its own payload shape directly:
        ``mode: "dashboard"``, ``cards``/``filter_label`` in place of the
        usual single ``chart``/a multi-row ``rows``, and ``sql: None``
        (there is no single query to show).  ``plan["filters"]`` is the
        plain dict :func:`agent.dashboard.extract_filters_from_text`
        produces (or, for a real tool-calling LLM, whatever
        ``agent.tools.build_dashboard`` receives via the agentic loop
        instead — that path never reaches this method).
        """
        from agent.dashboard import (
            DashboardFilters,
            FilterValidationError,
            get_data_horizon,
            parse_relative_range,
            run_dashboard,
        )

        raw = plan.get("filters") or {}
        range_label: Optional[str] = None
        date_start = date_end = None
        relative_text = raw.get("relative_range_text")
        if relative_text:
            resolved = parse_relative_range(relative_text, get_data_horizon(self.driver))
            if resolved:
                date_start, date_end, range_label = resolved

        try:
            filters = DashboardFilters(
                date_start=date_start,
                date_end=date_end,
                market=raw.get("market"),
                channel=raw.get("channel"),
                device=raw.get("device"),
                platform=raw.get("platform"),
            )
            dashboard_result = run_dashboard(self.driver, filters, range_label=range_label)
        except FilterValidationError as exc:
            return {
                "question": question,
                "mode": "error",
                "sql": None,
                "rows": [],
                "chart": None,
                "answer": f"I couldn't build that dashboard — invalid {exc.field}: {exc}",
            }

        answer = dashboard_result["headline_summary"]
        translated = self._maybe_translate(answer, narrate_language)
        return {
            "question": question,
            "mode": "dashboard",
            "sql": None,
            "rows": [],
            "chart": None,
            "cards": dashboard_result["cards"],
            "filter_label": dashboard_result["filter_label"],
            "applied_range": dashboard_result.get("applied_range"),
            "answer": translated or answer,
        }

    def _maybe_translate(self, text: str, narrate_language: str) -> Optional[str]:
        """Best-effort narrative translation; no-op unless AnthropicLLM is live."""
        if not narrate_language or not narrate_language.lower().startswith("tr"):
            return None
        translate = getattr(self.llm, "translate", None)
        if not callable(translate):
            return None
        try:
            return translate(text, "tr")
        except Exception:  # noqa: BLE001 - narration is best-effort, never fatal
            return None

    # -------------------------------------------------------------- helpers

    def _payload(
        self,
        question: Optional[str],
        mode: str,
        sql: str,
        df: pd.DataFrame,
        chart: Optional[dict],
        metric: Optional[dict],
        hint: Optional[str] = None,
    ) -> dict:
        rows = df.head(_MAX_DISPLAY_ROWS).to_dict(orient="records")
        answer = self._narrate(df, chart, metric, hint)
        payload: dict = {
            "question": question,
            "mode": mode,
            # Display-only formatting, applied at the last moment before this
            # payload is serialized — `sql` (and metric["sql"]) above is what
            # was actually executed and stays untouched.
            "sql": format_sql_for_display(sql),
            "rows": rows,
            "chart": chart,
            "answer": answer,
        }
        if metric is not None:
            payload["metric_key"] = metric["key"]
            payload["title"] = metric["title"]
            if metric.get("consent_note"):
                payload["consent_note"] = metric["consent_note"].strip()
        return payload

    @staticmethod
    def _narrate(
        df: pd.DataFrame,
        chart: Optional[dict],
        metric: Optional[dict],
        hint: Optional[str],
    ) -> str:
        """Build a short plain-English summary of the result table."""
        parts: list[str] = []
        if metric is not None:
            parts.append(metric["title"] + ".")

        if df.empty:
            parts.append("The query returned no rows.")
        else:
            numeric_cols = [
                c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])
            ]
            label_cols = [c for c in df.columns if c not in numeric_cols]
            # Pick the most meaningful measure: the chart's y, else a
            # rate-like column, else the last numeric column.
            measure: Optional[str] = None
            if chart and chart.get("y") in numeric_cols:
                measure = chart["y"]
            if measure is None:
                rate_like = [
                    c for c in numeric_cols
                    if any(t in c.lower() for t in ("rate", "share", "ratio"))
                ]
                measure = rate_like[-1] if rate_like else (
                    numeric_cols[-1] if numeric_cols else None
                )

            if measure is None or not label_cols:
                if len(df) == 1 and measure is not None:
                    parts.append(f"{measure} = {_fmt(df.iloc[0][measure])}.")
                else:
                    parts.append(f"Returned {len(df)} row(s).")
            elif (chart or {}).get("type") == "funnel":
                stages = [
                    f"{r[label_cols[-1]]}: {_fmt(r[measure])}"
                    for _, r in df.iterrows()
                ]
                parts.append(" -> ".join(stages) + ".")
            elif (chart or {}).get("type") == "line":
                first, last = df.iloc[0], df.iloc[-1]
                direction = (
                    "up" if last[measure] > first[measure]
                    else "down" if last[measure] < first[measure]
                    else "flat"
                )
                parts.append(
                    f"{len(df)} periods, from {_fmt(first[measure])} "
                    f"({first[label_cols[0]]}) to {_fmt(last[measure])} "
                    f"({last[label_cols[0]]}) — trending {direction}."
                )
            else:
                ordered = df.sort_values(measure, ascending=False)
                top, bottom = ordered.iloc[0], ordered.iloc[-1]

                def label(row: pd.Series) -> str:
                    return " / ".join(str(row[c]) for c in label_cols)

                sentence = (
                    f"Highest {measure}: {label(top)} at {_fmt(top[measure])}"
                )
                if len(df) > 1:
                    sentence += (
                        f"; lowest: {label(bottom)} at {_fmt(bottom[measure])}"
                    )
                parts.append(sentence + f" ({len(df)} groups).")

        if hint:
            parts.append(str(hint))
        if metric is not None and metric.get("consent_note"):
            parts.append("Note: " + " ".join(metric["consent_note"].split()))
        return " ".join(parts)
