"""Module M11: the filterable-KPI dashboard behind "build me a KPI
dashboard for the last 3 months for Germany" as one natural-language
command.

This module is deliberately self-contained (no import of ``agent.agent``
or ``agent.tools``, to keep the import graph acyclic — see the module
docstring notes at each call site) and covers, top to bottom:

* :class:`DashboardFilters` — the shared filters model (date range,
  market, channel, device, platform), used by both the REST request body
  (``app.main``) and the agentic tool (``agent.tools.build_dashboard``).
* Deterministic, driver-free text parsing (:func:`is_dashboard_intent`,
  :func:`extract_filters_from_text`, :func:`parse_relative_range`) — the
  "keyword/offline demo" path (module spec item 4), used by
  :class:`agent.llm.KeywordLLM` and :mod:`agent.testing`.
* Safe SQL composition (:func:`build_where`) from an ALLOWLIST of columns
  (``config/dashboard_kpis.json``'s ``filter_columns``, never anything
  derived from user text) plus validated, quoted values — never raw user
  text spliced into SQL.
* Distinct-value validation (:func:`validate_filters`), cached per driver.
* :func:`run_dashboard` — runs every registered KPI template
  (``config/dashboard_kpis.json``) against the two M11 gold cubes
  (``gold.web_funnel_daily_cube`` / ``gold.journey_daily_cube``, see
  ``sql/medallion.sql``) with this request's filters applied, and returns
  ready-to-render cards (same shape as ``POST /api/dashboard``'s legacy
  cards: key/title/chart/rows/answer/consent_note) plus a human-readable
  filter label and a one-paragraph headline summary.

Relative date ranges ("last 3 months", "last 3 days") are ALWAYS anchored
to the MAX event timestamp actually present in the data
(:func:`get_data_horizon`), never wall-clock "today" — the dataset is a
static, dated snapshot.

M11-fix: the two gold cubes are DAY-grained (``day_date``), not week-
grained — a real run of "the last 3 days for Germany" against the
original week-grained cubes silently produced a "last 6 weeks" dashboard
instead, because a week bucket cannot answer a day-level question and the
planner quietly substituted a range it COULD answer. Two things fix this
together: the cubes are now daily (finest grain a filter can be cut at —
see sql/medallion.sql), and :func:`parse_relative_range` now understands
days as their own unit, so "last 3 days" is no longer unparseable in the
first place. Belt-and-suspenders: an unparseable relative-range phrase
now raises :class:`UnparseableRangeError` (a structured, retryable error
naming the supported units) rather than being silently dropped — see
:func:`resolve_relative_range`.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from pydantic import BaseModel, ConfigDict

from agent.sqlfmt import format_sql_for_display

# ---------------------------------------------------------------------------
# Filters model — shared by app.main's request body and agent.tools' tool.
# ---------------------------------------------------------------------------
class DashboardFilters(BaseModel):
    """All fields optional; an unset field means "no filter on this
    dimension." Dates are plain ``date`` (pydantic parses/validates an
    ISO ``"YYYY-MM-DD"`` string automatically, which is also how a 422
    surfaces for a malformed date at the FastAPI layer)."""

    model_config = ConfigDict(extra="forbid")

    date_start: Optional[date] = None
    date_end: Optional[date] = None
    market: Optional[str] = None
    channel: Optional[str] = None
    device: Optional[str] = None
    platform: Optional[str] = None


class FilterValidationError(ValueError):
    """Raised by :func:`validate_filters` / :func:`build_where` for a
    filter value that fails validation. ``field`` names the offending
    request field, so a caller (``app.main``, ``agent.tools``) can surface
    exactly which filter was bad rather than a generic 4xx."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(message)


# ---------------------------------------------------------------------------
# Deterministic text parsing — no driver access, safe to call from
# agent.llm.KeywordLLM.plan() (which has no driver) and agent.testing.
# ---------------------------------------------------------------------------
#: Kept intentionally narrow (not "overview") — agent.llm.KeywordLLM already
#: maps "overview"/"funnel" to the single funnel_overview metric, and a
#: dashboard-intent match here takes priority over that in FunnelAgent.ask.
#: No trailing `\b` on "kokpit" -- Turkish attaches suffixes directly
#: ("kokpiti", "kokpitini", ...), so a boundary would miss the ordinary
#: inflected forms an analyst actually types.
_DASHBOARD_INTENT_RE = re.compile(r"\b(kpi\s*dashboard|kpi\s*board|dashboard|kokpit)", re.IGNORECASE)

#: M11-fix: "days"/"gün" added after a real run of "the last 3 days for
#: Germany" silently produced a "last 6 weeks" dashboard instead — the
#: original regex only knew months/weeks, so a day-level ask had nothing to
#: match and the (real-LLM) planner quietly substituted a unit it COULD
#: express rather than surfacing the mismatch. Days are also now the two
#: cubes' actual grain (see sql/medallion.sql) — a day-level filter is no
#: longer merely parseable, it is answerable too.
_RELATIVE_RANGE_RE = re.compile(
    r"\b(?:last|past|son)\s+(\d{1,2})\s*(days?|months?|weeks?|gün|ay|hafta)\b", re.IGNORECASE
)

#: Exact-case 2-letter market codes only (per the M11 spec) — matching
#: lowercase "us"/"uk" case-insensitively would false-positive on the
#: common English words "us"/"uk" appearing in ordinary chat text.
_MARKET_CODE_RE = re.compile(r"\b(DE|UK|US)\b")

#: Convenience beyond the literal "2-letter markets" spec: recognizes the
#: country names an analyst would actually type (e.g. the flagship demo
#: phrase "... for Germany"), mapped to the same DE/UK/US vocabulary.
#: Case-insensitive, unlike the 2-letter codes above, because a full
#: country name is not an ordinary-English-word collision risk.
_MARKET_ALIASES: dict[str, str] = {
    "united kingdom": "UK",
    "great britain": "UK",
    "germany": "DE",
    "deutschland": "DE",
    "britain": "UK",
    "england": "UK",
    "america": "US",
    "usa": "US",
}

#: Channel words -> the mapped channel vocabulary shared by both M11 cubes
#: (see sql/medallion.sql's web_funnel_daily_cube comment). Longest phrase
#: wins on overlap (checked in length order below).
_CHANNEL_WORDS: dict[str, str] = {
    "brand search": "paid_search_brand",
    "brand_search": "paid_search_brand",
    "retargeting": "retargeting_meta",
    "tiktok": "paid_social_tiktok",
    "facebook": "paid_social_meta",
    "organic": "organic_direct",
    "direct": "organic_direct",
    "meta": "paid_social_meta",
}

_DEVICE_WORDS: tuple[str, ...] = ("desktop", "mobile", "tablet")

_PLATFORM_WORDS: dict[str, str] = {"ios": "iOS", "android": "Android"}


def is_dashboard_intent(text: str) -> bool:
    """True when ``text`` reads as a dashboard/KPI-board request rather
    than a single-metric question (see the M11 agentic system-prompt
    rule in agent.agentic for the same distinction for a real LLM)."""
    return bool(_DASHBOARD_INTENT_RE.search(text or ""))


def _subtract_months(anchor: date, months: int) -> date:
    """``anchor`` minus ``months`` calendar months, clamping the day to
    the shorter target month (e.g. Mar 31 - 1 month -> Feb 28/29)."""
    total = anchor.month - 1 - months
    year = anchor.year + total // 12
    month = total % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def parse_relative_range(text: str, horizon: date) -> Optional[tuple[date, date, str]]:
    """Parse a "last/past N days/weeks/months" phrase in ``text``, anchored
    to ``horizon`` (never wall-clock today — see :func:`get_data_horizon`).

    Returns ``(date_start, date_end, human_label)`` or ``None`` if no such
    phrase is present. ``date_end`` is always ``horizon`` itself.

    Boundary semantics (documented per the M11-fix request): for EVERY
    unit, ``date_start = horizon - N <unit>`` and both ends are inclusive
    (``build_where`` applies ``>= date_start`` and ``<= date_end``) — this
    is the SAME convention "last N weeks"/"last N months" already used
    before days existed, kept uniform rather than special-cased. That
    means "last N days" spans N+1 calendar days inclusive (e.g. "last 3
    days" against a horizon of Aug 30 covers Aug 27-30: 4 days), exactly
    as "last 1 week" already spans 8 days (horizon-7..horizon), not a
    clean 7. A caller who wants a strict "trailing N calendar days,
    excluding today" window would need N-1 here; that is deliberately not
    what this function does, for consistency with the pre-existing units.
    """
    match = _RELATIVE_RANGE_RE.search(text or "")
    if not match:
        return None
    n = int(match.group(1))
    unit = match.group(2).lower()
    is_day = unit.startswith("day") or unit == "gün"
    is_week = unit.startswith("week") or unit == "hafta"
    date_end = horizon
    if is_day:
        date_start = horizon - timedelta(days=n)
        label = f"Last {n} day" + ("" if n == 1 else "s")
    elif is_week:
        date_start = horizon - timedelta(weeks=n)
        label = f"Last {n} week" + ("" if n == 1 else "s")
    else:
        date_start = _subtract_months(horizon, n)
        label = f"Last {n} month" + ("" if n == 1 else "s")
    return date_start, date_end, label


class UnparseableRangeError(ValueError):
    """Raised by :func:`resolve_relative_range` when ``text`` cannot be
    parsed as a relative date range at all (M11-fix "honesty rule").

    Carries ``phrase`` (the raw text that failed) and ``supported_units``
    so a caller — chiefly ``agent.tools.ToolRegistry.build_dashboard``,
    where an LLM supplies ``relative_range`` as an explicit "this names a
    date range" argument — can surface exactly what IS understood and
    retry with a supported phrase, instead of the request being silently
    dropped (the exact failure mode the M11-fix was written to close: a
    day-level ask silently turning into a several-week-wide dashboard with
    no indication anything was substituted).
    """

    SUPPORTED_UNITS: tuple[str, ...] = ("days", "weeks", "months")

    def __init__(self, phrase: str) -> None:
        self.phrase = phrase
        self.supported_units = self.SUPPORTED_UNITS
        super().__init__(
            f"Could not parse {phrase!r} as a relative date range. Supported "
            f"units: {', '.join(self.supported_units)} (e.g. 'last 3 days', "
            "'last 6 weeks', 'last 3 months', 'son 3 gün')."
        )


def resolve_relative_range(text: str, horizon: date) -> tuple[date, date, str]:
    """Like :func:`parse_relative_range`, but RAISES
    :class:`UnparseableRangeError` instead of returning ``None``.

    Use this (rather than calling :func:`parse_relative_range` directly)
    at any call site where ``text`` arrives as an explicit "the caller
    means this to be a date-range phrase" value — e.g. the
    ``build_dashboard`` tool's ``relative_range`` argument — so an
    unsupported unit is a structured, retryable error the LLM sees,
    never a silently-ignored filter.
    """
    resolved = parse_relative_range(text, horizon)
    if resolved is None:
        raise UnparseableRangeError(text)
    return resolved


def extract_filters_from_text(text: str) -> dict[str, str]:
    """Regex-only extraction of dashboard filters from free text.

    Returns a plain dict with any of the keys ``relative_range_text``
    (the matched phrase, e.g. ``"last 3 months"`` — NOT yet resolved to
    concrete dates, since that needs :func:`get_data_horizon`'s driver
    access), ``market``, ``channel``, ``device``, ``platform`` — only the
    keys actually detected. Never raises; an unrecognized question
    returns ``{}``.
    """
    text = text or ""
    lower = text.lower()
    out: dict[str, str] = {}

    range_match = _RELATIVE_RANGE_RE.search(text)
    if range_match:
        out["relative_range_text"] = range_match.group(0)

    market_match = _MARKET_CODE_RE.search(text)
    if market_match:
        out["market"] = market_match.group(1).upper()
    else:
        for alias in sorted(_MARKET_ALIASES, key=len, reverse=True):
            if alias in lower:
                out["market"] = _MARKET_ALIASES[alias]
                break

    for phrase in sorted(_CHANNEL_WORDS, key=len, reverse=True):
        if phrase in lower:
            out["channel"] = _CHANNEL_WORDS[phrase]
            break

    for word in _DEVICE_WORDS:
        if re.search(rf"\b{word}\b", lower):
            out["device"] = word
            break

    for word, value in _PLATFORM_WORDS.items():
        if re.search(rf"\b{word}\b", lower):
            out["platform"] = value
            break

    return out


# ---------------------------------------------------------------------------
# Data horizon (relative-range anchor) — cached per driver instance.
# ---------------------------------------------------------------------------
_HORIZON_CACHE: dict[int, date] = {}


def get_data_horizon(driver: Any) -> date:
    """The latest event date across web_events/app_events for ``driver``,
    cached per driver instance (this is a static demo dataset — the
    horizon never moves within one process's life). Relative ranges
    ("last 3 months") anchor to this, NEVER to wall-clock today."""
    key = id(driver)
    if key not in _HORIZON_CACHE:
        df = driver.query(
            "SELECT MAX(ts) AS max_ts FROM ("
            "SELECT event_timestamp AS ts FROM web_events "
            "UNION ALL SELECT event_timestamp AS ts FROM app_events"
            ") combined"
        )
        max_ts = df.iloc[0]["max_ts"]
        _HORIZON_CACHE[key] = (
            max_ts.date() if hasattr(max_ts, "date") else pd.Timestamp(max_ts).date()
        )
    return _HORIZON_CACHE[key]


# ---------------------------------------------------------------------------
# Distinct-value validation — cached per driver instance.
# ---------------------------------------------------------------------------
_DISTINCT_VALUE_QUERIES: dict[str, str] = {
    "market": (
        "SELECT market AS value FROM gold.web_funnel_daily_cube "
        "UNION SELECT market AS value FROM gold.journey_daily_cube"
    ),
    "channel": (
        "SELECT channel AS value FROM gold.web_funnel_daily_cube "
        "UNION SELECT acquisition_channel AS value FROM gold.journey_daily_cube"
    ),
    "device": "SELECT DISTINCT device_category AS value FROM gold.web_funnel_daily_cube",
    "platform": "SELECT DISTINCT platform AS value FROM gold.journey_daily_cube",
}

_DISTINCT_VALUE_CACHE: dict[int, dict[str, frozenset[str]]] = {}


def _distinct_values(driver: Any, field: str) -> frozenset[str]:
    per_driver = _DISTINCT_VALUE_CACHE.setdefault(id(driver), {})
    if field not in per_driver:
        df = driver.query(_DISTINCT_VALUE_QUERIES[field])
        per_driver[field] = frozenset(str(v) for v in df["value"].dropna().tolist())
    return per_driver[field]


def validate_filters(filters: DashboardFilters, driver: Any) -> None:
    """Raise :class:`FilterValidationError` for any filter value that
    cannot possibly return rows: market/channel/device/platform are
    checked against the actual distinct values in the two M11 cubes
    (cheap ``SELECT DISTINCT``, cached per driver — see above); the date
    range is checked for internal consistency. Never raises for an
    entirely-unset ``filters`` (every field ``None``)."""
    if filters.date_start and filters.date_end and filters.date_start > filters.date_end:
        raise FilterValidationError(
            "date_end", f"date_end ({filters.date_end}) must be on or after date_start ({filters.date_start})."
        )
    for field in ("market", "channel", "device", "platform"):
        value = getattr(filters, field)
        if value is None:
            continue
        allowed = _distinct_values(driver, field)
        if value not in allowed:
            raise FilterValidationError(
                field,
                f"Unknown {field} {value!r}. Known values: {', '.join(sorted(allowed))}.",
            )


# ---------------------------------------------------------------------------
# Safe WHERE composition — allowlisted columns (from config/dashboard_
# kpis.json, never user input) + validated, quoted values.
# ---------------------------------------------------------------------------
#: Defense-in-depth on top of validate_filters()'s distinct-value check:
#: every real value in this dataset (channel/market/device/platform codes)
#: matches this class; anything else (e.g. an injection attempt) is
#: rejected here even if it somehow slipped past validate_filters.
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9 _\-./]+$")


def _quote_value(field: str, value: str) -> str:
    if not isinstance(value, str) or not _SAFE_VALUE_RE.match(value):
        raise FilterValidationError(field, f"{field} value {value!r} contains characters that are not allowed.")
    return "'" + value.replace("'", "''") + "'"


def build_where(filters: DashboardFilters, filter_columns: dict[str, str]) -> str:
    """Compose a safe SQL ``WHERE`` body for one KPI template.

    ``filter_columns`` is the template's own allowlist (straight from
    ``config/dashboard_kpis.json``, never derived from the request) mapping
    generic filter names to the actual column on that template's cube; a
    filter this cube does not carry (missing from the map) is silently not
    applied to this one card. Every value that IS applied has already
    passed :func:`validate_filters` and is quoted again here as a second,
    independent layer of protection — this function never splices a raw
    request string into SQL.
    """
    clauses = ["1=1"]
    if filters.date_start and "date_start" in filter_columns:
        clauses.append(f"{filter_columns['date_start']} >= DATE '{filters.date_start.isoformat()}'")
    if filters.date_end and "date_end" in filter_columns:
        clauses.append(f"{filter_columns['date_end']} <= DATE '{filters.date_end.isoformat()}'")
    for field in ("market", "channel", "device", "platform"):
        value = getattr(filters, field)
        if value is not None and field in filter_columns:
            clauses.append(f"{filter_columns[field]} = {_quote_value(field, value)}")
    return " AND ".join(clauses)


# ---------------------------------------------------------------------------
# KPI template registry (config/dashboard_kpis.json).
# ---------------------------------------------------------------------------
_KPI_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "dashboard_kpis.json"
_kpi_templates_cache: Optional[list[dict]] = None


def load_kpi_templates(path: Optional[Path] = None) -> list[dict]:
    """The ~10 dashboard KPI templates from ``config/dashboard_kpis.json``,
    cached (default path only)."""
    global _kpi_templates_cache
    use_default = path is None
    if use_default and _kpi_templates_cache is not None:
        return _kpi_templates_cache
    with open(path or _KPI_CONFIG_PATH, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    templates = data["kpis"]
    if use_default:
        _kpi_templates_cache = templates
    return templates


# ---------------------------------------------------------------------------
# Rendering: cards, filter label, headline summary.
# ---------------------------------------------------------------------------
def build_filter_label(filters: DashboardFilters, range_label: Optional[str] = None) -> str:
    """A short, human-readable label for the active filter, e.g.
    ``"Last 3 months · DE"`` — always English (this app's convention:
    deterministic/backend-composed strings stay English; see
    agent.agent.FunnelAgent's narration and api_contract.md's language
    note). ``"All data"`` when every field is unset."""
    parts: list[str] = []
    if range_label:
        parts.append(range_label)
    elif filters.date_start or filters.date_end:
        start = filters.date_start.isoformat() if filters.date_start else "…"
        end = filters.date_end.isoformat() if filters.date_end else "…"
        parts.append(f"{start} → {end}")
    for value in (filters.market, filters.channel, filters.device, filters.platform):
        if value:
            parts.append(value)
    return " · ".join(parts) if parts else "All data"


def _fmt_num(value: Any) -> str:
    if isinstance(value, float):
        if value != value:  # NaN
            return "n/a"
        if 0.0 <= value <= 1.0:
            return f"{value * 100:.1f}%"
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _narrate_card(df: pd.DataFrame, template: dict) -> str:
    """A short plain-English summary of one KPI card's result table —
    intentionally self-contained (mirrors, but does not import,
    agent.agent.FunnelAgent._narrate's spirit) so this module stays free
    of any import on agent.agent (see the module docstring)."""
    title = template["title"]
    if df.empty:
        return f"{title}. No rows under this filter."
    chart = template.get("chart") or {}
    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    label_cols = [c for c in df.columns if c not in numeric_cols]
    measure = chart.get("y") if chart.get("y") in numeric_cols else None
    if measure is None:
        measure = numeric_cols[-1] if numeric_cols else None

    if measure is None or not label_cols:
        if len(df) == 1 and measure is not None:
            return f"{title}: {measure} = {_fmt_num(df.iloc[0][measure])}."
        return f"{title}. {len(df)} row(s)."

    if chart.get("type") == "funnel":
        stages = [f"{r[label_cols[-1]]}: {_fmt_num(r[measure])}" for _, r in df.iterrows()]
        return f"{title}. " + " -> ".join(stages) + "."

    if chart.get("type") == "line":
        first, last = df.iloc[0], df.iloc[-1]
        return (
            f"{title}: {len(df)} period(s), from {_fmt_num(first[measure])} to "
            f"{_fmt_num(last[measure])}."
        )

    ordered = df.sort_values(measure, ascending=False)
    top = ordered.iloc[0]
    label = " / ".join(str(top[c]) for c in label_cols)
    sentence = f"{title}. Highest {measure}: {label} at {_fmt_num(top[measure])}"
    if len(df) > 1:
        bottom = ordered.iloc[-1]
        bottom_label = " / ".join(str(bottom[c]) for c in label_cols)
        sentence += f"; lowest: {bottom_label} at {_fmt_num(bottom[measure])}"
    return sentence + f" ({len(df)} group(s))."


def _rows_json_safe(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe list of dicts (NaN/Inf -> null, dates ->
    ISO) — mirrors agent.tools._rows_to_json."""
    return json.loads(df.to_json(orient="records", date_format="iso"))


def _headline_summary(cards_by_key: dict[str, dict], filter_label: str) -> str:
    """One short paragraph summarizing headline numbers under the active
    filter — the M11 spec's "final answer text" for the deterministic
    (keyword) path; the agentic (real-LLM) path uses this only as one
    tool-result field among several, and composes its own final prose."""
    prefix = "KPI dashboard" if filter_label == "All data" else f"Filtered KPI dashboard ({filter_label})"
    sentences: list[str] = []

    funnel = cards_by_key.get("dash_funnel_stages")
    if funnel and funnel["rows"]:
        by_stage = {r["stage"]: r["users"] for r in funnel["rows"]}
        starts = by_stage.get("test_starts") or 0
        completes = by_stage.get("test_completes") or 0
        redirects = by_stage.get("store_redirects") or 0
        rate = f"{completes / starts * 100:.1f}%" if starts else "n/a"
        sentences.append(
            f"{starts:,.0f} test starts, {completes:,.0f} completions ({rate} conversion), "
            f"{redirects:,.0f} app-store redirects."
        )

    downloads = cards_by_key.get("dash_downloads_by_channel")
    pairing = cards_by_key.get("dash_pairing_by_channel")
    total_downloads = sum(r["downloads"] or 0 for r in downloads["rows"]) if downloads else 0
    total_paired = sum(r["paired_users"] or 0 for r in pairing["rows"]) if pairing else 0
    if downloads is not None or pairing is not None:
        sentences.append(
            f"{total_downloads:,.0f} linked downloads, {total_paired:,.0f} pairings "
            "among them (consented, linkable population)."
        )

    if not sentences:
        sentences.append("No rows matched this filter — try widening the date range or clearing a filter.")

    return f"{prefix}: " + " ".join(sentences)


def run_dashboard(driver: Any, filters: DashboardFilters, range_label: Optional[str] = None) -> dict:
    """Run every registered KPI template against the two M11 gold cubes
    with ``filters`` applied. Raises :class:`FilterValidationError` (never
    a raw DB exception) for an out-of-range filter value.

    Returns ``{"cards": [...], "filter_label": str, "headline_summary": str,
    "applied_range": {...} | None}`` — ``cards`` are in the same shape
    ``POST /api/dashboard``'s legacy (unfiltered) cards already use:
    key/title/chart/rows/answer/consent_note/sql, so the frontend renders
    both with one code path. ``sql`` (M11 addendum 2) is the exact
    statement executed for that card — the template with this request's
    WHERE composed in — reindented for display via
    :func:`agent.sqlfmt.format_sql_for_display`. ``applied_range``
    (M11-fix "honesty rule") is the
    date window actually applied, isolated from the compound
    ``filter_label`` string (which also bundles market/channel/device/
    platform) — ``{"start": iso, "end": iso, "label": range_label}`` when
    any date filter is active, else ``None``. This is the field a caller
    should echo verbatim when confirming to the user which range was
    actually used, rather than re-deriving it from ``filter_label`` text.
    """
    validate_filters(filters, driver)
    cards: list[dict] = []
    for template in load_kpi_templates():
        where = build_where(filters, template["filter_columns"])
        sql = template["sql"].replace("{{where}}", where)
        df = driver.query(sql)
        cards.append(
            {
                "key": template["key"],
                "title": template["title"],
                "chart": template.get("chart"),
                "rows": _rows_json_safe(df),
                "answer": _narrate_card(df, template),
                "consent_note": (template.get("consent_note") or ""),
                # M11 addendum 2: the exact statement actually executed
                # (WHERE composed for these filters), reindented for
                # display only via the shared sqlfmt choke point — never
                # the string that was run.
                "sql": format_sql_for_display(sql),
            }
        )
    filter_label = build_filter_label(filters, range_label=range_label)
    cards_by_key = {c["key"]: c for c in cards}
    applied_range = None
    if filters.date_start or filters.date_end:
        applied_range = {
            "start": filters.date_start.isoformat() if filters.date_start else None,
            "end": filters.date_end.isoformat() if filters.date_end else None,
            "label": range_label,
        }
    return {
        "cards": cards,
        "filter_label": filter_label,
        "headline_summary": _headline_summary(cards_by_key, filter_label),
        "applied_range": applied_range,
    }
