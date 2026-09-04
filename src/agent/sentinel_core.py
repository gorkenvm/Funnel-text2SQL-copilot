"""Pure-Python core of the sentinel — anomaly & schema-drift watchdog (module
M5, PDF task 5.5).

This module holds every piece of logic that is NOT SQL: reading/rendering
``sql/sentinel/*.sql``, turning their result rows into typed
:class:`Finding` objects against ``config/sentinel_registry.json``'s
thresholds, diffing the live schema snapshot against that registry, and
composing the (statistics-only) narrative fallback. It is imported by BOTH
``scripts/sentinel.py`` (the CLI/Job entry point) and
``notebooks/sentinel_job.ipynb`` (the Databricks Job wrapper) so the
detection logic lives exactly once — see the notebook's own header comment
for how it imports this module (or mirrors it, if the workspace layout
cannot import it).

Design philosophy (see docs/sentinel_design.md): STATISTICS DETECT, the LLM
only NARRATES. Every :class:`Finding` below is produced by SQL window
functions and plain Python threshold comparisons — never by an LLM call —
so the set of findings is identical whether or not a real LLM is
configured. Narration (:func:`narrate`) is the one place an LLM is allowed
to run, and only to phrase findings that already exist, never to invent
them.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

import pandas as pd

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

#: sql/sentinel/*.sql — the three versioned, read-only check queries.
SQL_DIR = Path(__file__).resolve().parents[2] / "sql" / "sentinel"
VOLUME_SQL_PATH = SQL_DIR / "daily_event_volumes.sql"
RATES_SQL_PATH = SQL_DIR / "daily_funnel_rates.sql"
SCHEMA_SQL_PATH = SQL_DIR / "schema_snapshot.sql"

#: config/sentinel_registry.json — the expected/baseline state (drift target).
DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "sentinel_registry.json"
)

#: The three severities a Finding can carry, low to high.
SEVERITIES: tuple[str, ...] = ("info", "warning", "critical")
_SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

#: Exit code per worst severity present (Job-friendly: 0 clean / 1 warnings /
#: 2 critical), plus the clean-run code when there are no findings at all.
EXIT_CODE_BY_WORST_SEVERITY = {None: 0, "info": 0, "warning": 1, "critical": 2}

#: Defaults for config/sentinel_registry.json's "thresholds" block, used
#: whenever a key is absent from the loaded registry (keeps old registry
#: files forward-compatible if a new threshold key is introduced later).
DEFAULT_THRESHOLDS: dict[str, float] = {
    # |deviation_z| at/above which a volume or rate series becomes a finding,
    # one tier per severity. Below band_multiplier_info, a series that is
    # inside its trailing 28-day band produces no finding at all.
    "band_multiplier_info": 1.5,
    "band_multiplier_warning": 2.5,
    "band_multiplier_critical": 4.0,
    # A series never gets flagged, however large |deviation_z| is, unless
    # its trailing band average is at least this many events/day (or the
    # as-of actual count is) -- this is the alert-fatigue guard: a tiny
    # series (e.g. 2 events/day) swings wildly in z-score terms on entirely
    # normal Poisson noise, and flagging that every day trains analysts to
    # ignore the sentinel altogether.
    "min_volume_floor": 5,
    # A series needs at least this many trailing days of history before its
    # band is trusted enough to flag anything (protects a metric that only
    # recently started being emitted from an immediate false alarm).
    "min_history_days": 14,
}

#: Days of lag between the true max event timestamp in the data and the
#: latest day :func:`default_as_of` will offer to score.
#:
#: Several event types here are naturally LAGGED behind an earlier event in
#: the same user journey: app_store_redirect and app_open both depend on a
#: prior session/first-open having had enough elapsed time to produce a
#: later action, and hearing_test_complete/result_screen_view/
#: app_store_redirect events for the LAST calendar day in a closed dataset
#: are inherently a partial day (the data simply stops mid-day). Scoring one
#: of those immature days against a mature 28-day band manufactures false
#: criticals on perfectly healthy data -- this is exactly the same
#: right-censoring idea sql/medallion.sql already applies to D30 retention
#: (silver.app_user_stages.censored), just at the day-volume level instead
#: of the user-cohort level, and empirically the largest deviation across
#: every series in this dataset settles under 1.2 std-dev by this lag (see
#: reports/m5_test_report.txt for the measurements that picked this number).
EVENT_MATURITY_BUFFER_DAYS = 6

#: Raw event tables this sentinel governs -- READ ONLY. Sentinel never
#: writes to bronze/silver/gold or these three raw tables.
RAW_TABLES: tuple[str, ...] = ("web_events", "app_events", "id_bridge")

#: Which "source" label (see daily_event_volumes.sql) each raw table feeds.
_TABLE_SOURCE = {"web_events": "web", "app_events": "app"}


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One statistics- or registry-derived observation.

    Never constructed from an LLM response -- see the module docstring.
    """

    check: str  # e.g. "daily_event_volume", "schema_columns", "missing_event_today"
    severity: str  # one of SEVERITIES
    subject: str  # short stable id, e.g. "app:hearing_aid_paired:Android"
    message: str  # human-readable, self-contained sentence
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITY_RANK:
            raise ValueError(f"Unknown severity '{self.severity}'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "subject": self.subject,
            "message": self.message,
            "details": self.details,
        }


def worst_severity(findings: list[Finding]) -> Optional[str]:
    """The highest-ranked severity present, or None if `findings` is empty."""
    if not findings:
        return None
    return max((f.severity for f in findings), key=_SEVERITY_RANK.get)


def exit_code(findings: list[Finding]) -> int:
    """Job-friendly exit code: 0 clean, 1 warnings, 2 critical."""
    return EXIT_CODE_BY_WORST_SEVERITY[worst_severity(findings)]


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Worst severity first, then a stable (check, subject) order."""
    return sorted(
        findings, key=lambda f: (-_SEVERITY_RANK[f.severity], f.check, f.subject)
    )


# ---------------------------------------------------------------------------
# SQL loading / templating -- mirrors agent.medallion's {{token}} philosophy,
# kept to the single {{as_of}} variable these checks need.
# ---------------------------------------------------------------------------

_AS_OF_TOKEN = "{{as_of}}"
_CHECK_MARKER = re.compile(r"^--\s*@check:\s*(\w+)\s*$")


def read_sql(path: Path) -> str:
    """Read one sql/sentinel/*.sql file as text."""
    return path.read_text(encoding="utf-8")


def render_as_of(sql_text: str, as_of: str) -> str:
    """Substitute the single {{as_of}} template token with a DATE literal.

    ``as_of`` must already be an ISO ``YYYY-MM-DD`` string; it is inlined as
    a bare literal exactly like sql/medallion.sql inlines {{raw}} -- both are
    trusted, caller-controlled configuration, never end-user input.
    """
    if _AS_OF_TOKEN not in sql_text:
        raise ValueError("SQL text has no {{as_of}} token to render")
    return sql_text.replace(_AS_OF_TOKEN, as_of)


def load_named_statements(sql_text: str) -> dict[str, str]:
    """Split a ``-- @check: <name>`` annotated multi-statement file.

    Mirrors agent.medallion.parse_statements's full-line '--' comment
    stripping, plus one convention of its own: a marker comment line
    ``-- @check: <name>`` immediately introduces the next statement and
    names it, so callers can fetch a block by name instead of by position.
    Lines before the first marker, and any other '--' comment line, are
    dropped. A trailing ';' on a statement is stripped.
    """
    statements: dict[str, str] = {}
    current_name: Optional[str] = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_name, current_lines
        if current_name is not None:
            body = "\n".join(current_lines).strip()
            if body.endswith(";"):
                body = body[:-1].strip()
            if body:
                statements[current_name] = body
        current_name, current_lines = None, []

    for line in sql_text.splitlines():
        marker = _CHECK_MARKER.match(line.strip())
        if marker:
            flush()
            current_name = marker.group(1)
            continue
        if line.lstrip().startswith("--"):
            continue
        if current_name is not None:
            current_lines.append(line)
    flush()
    return statements


class Queryable(Protocol):
    """Anything with agent.db.BaseDriver's `query(sql) -> DataFrame` shape."""

    def query(self, sql: str) -> pd.DataFrame: ...


def default_as_of(driver: Queryable) -> str:
    """The latest day :func:`run_checks` is safe to score by default.

    Max observed event date across web_events/app_events, minus
    :data:`EVENT_MATURITY_BUFFER_DAYS` (see that constant's docstring for
    why the true max date itself is not a safe default). Returns an ISO
    ``YYYY-MM-DD`` string.
    """
    df = driver.query(
        "SELECT MAX(d) AS max_day FROM ("
        "  SELECT CAST(MAX(event_timestamp) AS DATE) AS d FROM web_events"
        "  UNION ALL"
        "  SELECT CAST(MAX(event_timestamp) AS DATE) AS d FROM app_events"
        ") AS horizons"
    )
    max_day = pd.Timestamp(df["max_day"].iloc[0]).date()
    return (max_day - dt.timedelta(days=EVENT_MATURITY_BUFFER_DAYS)).isoformat()


# ---------------------------------------------------------------------------
# Registry (config/sentinel_registry.json) load/save
# ---------------------------------------------------------------------------


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    """Load the expected-state registry, filling in any missing threshold
    keys from :data:`DEFAULT_THRESHOLDS` so an older registry file on disk
    keeps working after a new threshold key is introduced."""
    registry = json.loads(path.read_text(encoding="utf-8"))
    thresholds = dict(DEFAULT_THRESHOLDS)
    thresholds.update(registry.get("thresholds") or {})
    registry["thresholds"] = thresholds
    return registry


def build_registry(
    driver: Queryable,
    as_of_reference: Optional[str] = None,
    thresholds: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """Bootstrap a fresh registry from the CURRENT state of `driver`.

    Used once to generate config/sentinel_registry.json's initial content
    from the real data (never hand-typed), and reused by tests that need a
    baseline registry matching an in-memory tampered connection rather than
    the shipped file.
    """
    schema_stmts = load_named_statements(read_sql(SCHEMA_SQL_PATH))
    columns_df = driver.query(schema_stmts["columns"])
    event_names_df = driver.query(schema_stmts["event_names"])
    app_versions_df = driver.query(schema_stmts["app_versions"])

    expected_schema: dict[str, dict[str, dict[str, str]]] = {}
    for table_name, group in columns_df.groupby("table_name"):
        columns = dict(zip(group["column_name"], group["data_type"]))
        expected_schema[table_name] = {"columns": columns}

    expected_event_names: dict[str, list[str]] = {}
    for table_name, group in event_names_df.groupby("table_name"):
        expected_event_names[table_name] = sorted(group["event_name"].tolist())

    expected_app_versions = sorted(app_versions_df["app_version"].tolist())

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "as_of_reference": as_of_reference or default_as_of(driver),
        "expected_schema": expected_schema,
        "expected_event_names": expected_event_names,
        "expected_app_versions": expected_app_versions,
        "thresholds": {**DEFAULT_THRESHOLDS, **(thresholds or {})},
    }


# ---------------------------------------------------------------------------
# Statistical checks: daily_event_volumes.sql / daily_funnel_rates.sql rows
# -> Findings, purely from band numbers the SQL already computed plus the
# registry's thresholds. No SQL is written here, only arithmetic.
# ---------------------------------------------------------------------------


def _band_severity(
    deviation_z: Optional[float],
    volume_signal: float,
    history_days: float,
    thresholds: dict[str, float],
) -> Optional[str]:
    """Shared banding rule for both the volume and the rate check.

    Returns None (no finding) when the band has no usable spread yet, when
    there isn't enough trailing history, or when the series is too small
    for a z-score to mean anything (the min_volume_floor alert-fatigue
    guard) -- otherwise the severity tier |deviation_z| lands in.
    """
    if deviation_z is None or pd.isna(deviation_z):
        return None
    if history_days < thresholds["min_history_days"]:
        return None
    if volume_signal < thresholds["min_volume_floor"]:
        return None
    az = abs(deviation_z)
    if az >= thresholds["band_multiplier_critical"]:
        return "critical"
    if az >= thresholds["band_multiplier_warning"]:
        return "warning"
    if az >= thresholds["band_multiplier_info"]:
        return "info"
    return None


def _as_date_str(value: Any) -> str:
    """Render a DuckDB/Databricks DATE result (surfaces as a pandas
    Timestamp) as a bare ISO date string, never a "00:00:00" timestamp."""
    return pd.Timestamp(value).date().isoformat()


def evaluate_volume_findings(
    volume_df: pd.DataFrame, thresholds: dict[str, float]
) -> list[Finding]:
    """Turn daily_event_volumes.sql's as-of rows into volume Findings."""
    findings: list[Finding] = []
    for row in volume_df.itertuples(index=False):
        day = _as_date_str(row.day)
        volume_signal = max(row.actual_count, row.band_avg or 0)
        severity = _band_severity(
            row.deviation_z, volume_signal, row.band_days or 0, thresholds
        )
        if severity is None:
            continue
        direction = "above" if row.deviation_z >= 0 else "below"
        findings.append(
            Finding(
                check="daily_event_volume",
                severity=severity,
                subject=f"{row.source}:{row.event_name}:{row.segment}",
                message=(
                    f"{row.event_name} ({row.source} / {row.segment}) on {day}: "
                    f"actual={row.actual_count} is {abs(row.deviation_z):.2f}σ "
                    f"{direction} the trailing 28-day band "
                    f"({row.band_avg:.1f} ± {row.band_stddev:.1f})."
                ),
                details={
                    "day": day,
                    "event_name": row.event_name,
                    "segment": row.segment,
                    "source": row.source,
                    "actual_count": int(row.actual_count),
                    "band_avg": float(row.band_avg),
                    "band_stddev": float(row.band_stddev),
                    "band_days": int(row.band_days),
                    "deviation_z": float(row.deviation_z),
                },
            )
        )
    return findings


def evaluate_funnel_rate_findings(
    rates_df: pd.DataFrame, thresholds: dict[str, float]
) -> list[Finding]:
    """Turn daily_funnel_rates.sql's as-of rows into funnel-rate Findings."""
    findings: list[Finding] = []
    for row in rates_df.itertuples(index=False):
        day = _as_date_str(row.day)
        # A rate's "how much do we trust this number" signal is its
        # denominator (from_count), not the rate value itself.
        severity = _band_severity(
            row.deviation_z, row.from_count, row.band_days or 0, thresholds
        )
        if severity is None:
            continue
        direction = "above" if row.deviation_z >= 0 else "below"
        findings.append(
            Finding(
                check="daily_funnel_rate",
                severity=severity,
                subject=f"rate:{row.step}",
                message=(
                    f"{row.step} rate on {day}: actual={row.actual_rate:.1%} "
                    f"({row.to_count}/{row.from_count}) is {abs(row.deviation_z):.2f}σ "
                    f"{direction} the trailing 28-day band "
                    f"({row.band_avg:.1%} ± {row.band_stddev:.1%})."
                ),
                details={
                    "day": day,
                    "step": row.step,
                    "from_count": int(row.from_count),
                    "to_count": int(row.to_count),
                    "actual_rate": float(row.actual_rate),
                    "band_avg": float(row.band_avg),
                    "band_stddev": float(row.band_stddev),
                    "band_days": int(row.band_days),
                    "deviation_z": float(row.deviation_z),
                },
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Schema-drift checks: schema_snapshot.sql rows diffed against the registry.
# ---------------------------------------------------------------------------


def diff_columns(columns_df: pd.DataFrame, registry: dict[str, Any]) -> list[Finding]:
    """New/missing columns and type changes vs. registry["expected_schema"]."""
    findings: list[Finding] = []
    expected_schema = registry["expected_schema"]
    actual_by_table: dict[str, dict[str, str]] = {
        table_name: dict(zip(group["column_name"], group["data_type"]))
        for table_name, group in columns_df.groupby("table_name")
    }
    for table_name, expected in expected_schema.items():
        expected_columns: dict[str, str] = expected["columns"]
        actual_columns = actual_by_table.get(table_name, {})
        for column_name in sorted(set(actual_columns) - set(expected_columns)):
            findings.append(
                Finding(
                    check="schema_columns",
                    severity="warning",
                    subject=f"column:{table_name}.{column_name}",
                    message=(
                        f"New column '{column_name}' ({actual_columns[column_name]}) "
                        f"observed on {table_name}, not in the registry."
                    ),
                    details={"table": table_name, "column": column_name, "kind": "new"},
                )
            )
        for column_name in sorted(set(expected_columns) - set(actual_columns)):
            findings.append(
                Finding(
                    check="schema_columns",
                    severity="critical",
                    subject=f"column:{table_name}.{column_name}",
                    message=(
                        f"Expected column '{column_name}' is MISSING from "
                        f"{table_name}."
                    ),
                    details={"table": table_name, "column": column_name, "kind": "missing"},
                )
            )
        for column_name in sorted(set(expected_columns) & set(actual_columns)):
            expected_type = expected_columns[column_name]
            actual_type = actual_columns[column_name]
            if expected_type != actual_type:
                findings.append(
                    Finding(
                        check="schema_columns",
                        severity="warning",
                        subject=f"column:{table_name}.{column_name}",
                        message=(
                            f"Column '{table_name}.{column_name}' changed type: "
                            f"expected {expected_type}, observed {actual_type}."
                        ),
                        details={
                            "table": table_name,
                            "column": column_name,
                            "kind": "type_changed",
                            "expected_type": expected_type,
                            "actual_type": actual_type,
                        },
                    )
                )
    return findings


def diff_event_names(
    event_names_df: pd.DataFrame, registry: dict[str, Any]
) -> list[Finding]:
    """New/entirely-vanished event_name values vs. registry["expected_event_names"]."""
    findings: list[Finding] = []
    expected = registry["expected_event_names"]
    actual_by_table: dict[str, set[str]] = {
        table_name: set(group["event_name"])
        for table_name, group in event_names_df.groupby("table_name")
    }
    for table_name, expected_names in expected.items():
        actual_names = actual_by_table.get(table_name, set())
        for name in sorted(actual_names - set(expected_names)):
            findings.append(
                Finding(
                    check="schema_event_names",
                    severity="warning",
                    subject=f"event_name:{table_name}.{name}",
                    message=(
                        f"New event_name '{name}' observed in {table_name}, "
                        f"not in the registry."
                    ),
                    details={"table": table_name, "event_name": name, "kind": "new"},
                )
            )
        for name in sorted(set(expected_names) - actual_names):
            findings.append(
                Finding(
                    check="schema_event_names",
                    severity="critical",
                    subject=f"event_name:{table_name}.{name}",
                    message=(
                        f"Expected event_name '{name}' is no longer observed "
                        f"in ANY row of {table_name}."
                    ),
                    details={"table": table_name, "event_name": name, "kind": "vanished"},
                )
            )
    return findings


def diff_app_versions(
    app_versions_df: pd.DataFrame, registry: dict[str, Any]
) -> list[Finding]:
    """New app_version values vs. registry["expected_app_versions"].

    Deliberately one-directional: an OLD app_version disappearing (a
    completed rollout) is expected and healthy, so only a genuinely NEW,
    unregistered version is worth an analyst's eyes.
    """
    expected = set(registry["expected_app_versions"])
    actual = set(app_versions_df["app_version"])
    findings: list[Finding] = []
    for version in sorted(actual - expected):
        findings.append(
            Finding(
                check="schema_app_versions",
                severity="warning",
                subject=f"app_version:{version}",
                message=(
                    f"New app_version '{version}' observed in app_events, "
                    f"not in the registry."
                ),
                details={"app_version": version, "kind": "new"},
            )
        )
    return findings


def check_missing_events_today(
    volume_df: pd.DataFrame, registry: dict[str, Any], as_of: str
) -> list[Finding]:
    """Registry-driven presence check: did an expected event_name record
    ZERO rows anywhere on the as-of day?

    This complements the statistical volume check rather than duplicating
    it: daily_event_volumes.sql's GROUP BY can only ever produce a row for a
    (event_name, segment) combination that HAD at least one event that day
    -- a combination with zero events simply has no row to compute a
    z-score from, so a total, same-day outage would otherwise slip past the
    band check unnoticed. Comparing the registry's expected event names
    against which ones actually show up anywhere in `volume_df` on `as_of`
    closes that gap deterministically, independent of trailing-band math.
    """
    findings: list[Finding] = []
    if volume_df.empty:
        observed_by_source: dict[str, set[str]] = {}
    else:
        observed_by_source = {
            source: set(group["event_name"])
            for source, group in volume_df.groupby("source")
        }
    for table_name, expected_names in registry["expected_event_names"].items():
        source = _TABLE_SOURCE.get(table_name)
        if source is None:
            continue
        observed = observed_by_source.get(source, set())
        for name in sorted(set(expected_names) - observed):
            findings.append(
                Finding(
                    check="missing_event_today",
                    severity="critical",
                    subject=f"missing_today:{table_name}.{name}",
                    message=(
                        f"Expected event '{name}' has ZERO recorded rows in "
                        f"{table_name} on {as_of} (registry expects it daily)."
                    ),
                    details={"table": table_name, "event_name": name, "as_of": as_of},
                )
            )
    return findings


# ---------------------------------------------------------------------------
# End-to-end run
# ---------------------------------------------------------------------------


@dataclass
class SentinelRun:
    """Everything one sentinel invocation produced -- the Findings plus the
    raw check tables, so a caller (script, notebook, test) can render its
    own report without re-running the SQL."""

    as_of: str
    findings: list[Finding]
    volume_df: pd.DataFrame
    rates_df: pd.DataFrame
    columns_df: pd.DataFrame
    event_names_df: pd.DataFrame
    app_versions_df: pd.DataFrame
    registry: dict[str, Any]

    @property
    def exit_code(self) -> int:
        return exit_code(self.findings)


def run_checks(driver: Queryable, as_of: str, registry: dict[str, Any]) -> SentinelRun:
    """Run all three sql/sentinel/*.sql checks read-only via `driver`, and
    turn their rows into Findings against `registry`. This is the one
    function both scripts/sentinel.py and notebooks/sentinel_job.ipynb call.
    """
    thresholds = registry["thresholds"]

    volume_sql = render_as_of(read_sql(VOLUME_SQL_PATH), as_of)
    volume_df = driver.query(volume_sql)

    rates_sql = render_as_of(read_sql(RATES_SQL_PATH), as_of)
    rates_df = driver.query(rates_sql)

    schema_stmts = load_named_statements(read_sql(SCHEMA_SQL_PATH))
    columns_df = driver.query(schema_stmts["columns"])
    event_names_df = driver.query(schema_stmts["event_names"])
    app_versions_df = driver.query(schema_stmts["app_versions"])

    findings: list[Finding] = []
    findings += evaluate_volume_findings(volume_df, thresholds)
    findings += evaluate_funnel_rate_findings(rates_df, thresholds)
    findings += diff_columns(columns_df, registry)
    findings += diff_event_names(event_names_df, registry)
    findings += diff_app_versions(app_versions_df, registry)
    findings += check_missing_events_today(volume_df, registry, as_of)

    return SentinelRun(
        as_of=as_of,
        findings=sort_findings(findings),
        volume_df=volume_df,
        rates_df=rates_df,
        columns_df=columns_df,
        event_names_df=event_names_df,
        app_versions_df=app_versions_df,
        registry=registry,
    )


# ---------------------------------------------------------------------------
# Narration -- statistics detect, the LLM (if any) only narrates. See the
# module docstring; this is the ONLY function allowed to call an LLM, and it
# is passed the already-final findings list, never asked to produce numbers.
# ---------------------------------------------------------------------------


class NarratorLLM(Protocol):
    """The subset of agent.llm.LLMClient this module actually uses. Only
    AnthropicLLM/OpenAILLM implement chat_step; KeywordLLM does not, which
    is exactly how :func:`narrate` tells "a real LLM is available" apart
    from the deterministic fallback planner (see agent.llm's module
    docstring)."""

    def chat_step(self, messages: list[dict], tools: list[dict]) -> dict: ...


def template_summary(findings: list[Finding], as_of: str) -> str:
    """Deterministic, LLM-free executive summary -- the offline/no-API-key
    fallback, and what every test in tests/test_sentinel.py exercises."""
    if not findings:
        return (
            f"Sentinel run for {as_of}: all checked series are inside their "
            "trailing 28-day control bands and the live schema matches the "
            "registry. No action needed."
        )
    by_severity: dict[str, int] = {s: 0 for s in SEVERITIES}
    for f in findings:
        by_severity[f.severity] += 1
    counts_sentence = ", ".join(
        f"{by_severity[s]} {s}" for s in reversed(SEVERITIES) if by_severity[s]
    )
    worst = worst_severity(findings) or "info"
    headline_findings = [f for f in findings if f.severity == worst][:3]
    sentences = [
        f"Sentinel run for {as_of} found {len(findings)} finding(s): {counts_sentence}."
    ]
    if headline_findings:
        listed = "; ".join(f.message.rstrip(".") for f in headline_findings)
        sentences.append(f"Top {worst} item(s): {listed}.")
    check_kinds = sorted({f.check for f in findings})
    sentences.append(
        "Affected check(s): " + ", ".join(check_kinds) + "."
    )
    if worst == "critical":
        sentences.append(
            "Recommended action: an analyst should review this DRAFT report "
            "before any distribution, per the human-checkpoint design."
        )
    else:
        sentences.append(
            "No critical findings; still pending analyst approval before "
            "distribution, per the human-checkpoint design."
        )
    return " ".join(sentences)


_NARRATION_SYSTEM_PROMPT = (
    "You are the narration layer of a data-quality sentinel for a "
    "hearing-test analytics funnel. Every finding below was already "
    "computed by SQL statistics and registry diffing -- you narrate them, "
    "you never invent or alter a number. Write a 3-5 sentence executive "
    "summary for an analyst, in plain English, using ONLY the numbers and "
    "names given in the findings. Do not add counts, percentages, or "
    "series names that are not explicitly present in the input. If there "
    "are zero findings, say the run is clean in one sentence."
)


def narrate(
    findings: list[Finding], as_of: str, llm: Optional[NarratorLLM] = None
) -> str:
    """3-5 sentence executive summary of `findings` -- real-LLM narration
    when `llm` implements chat_step (AnthropicLLM/OpenAILLM), else the
    deterministic template. Never raises: any LLM failure (network, auth,
    malformed reply) falls back to the template rather than breaking the
    watchdog run, because narration is cosmetic and detection is not.
    """
    if llm is not None and hasattr(llm, "chat_step"):
        try:
            findings_payload = json.dumps([f.to_dict() for f in findings], indent=2)
            messages = [
                {"role": "system", "content": _NARRATION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"as_of: {as_of}\nfindings ({len(findings)}):\n{findings_payload}"
                    ),
                },
            ]
            reply = llm.chat_step(messages, tools=[])
            content = (reply or {}).get("content")
            if content and content.strip():
                return content.strip()
        except Exception:  # noqa: BLE001 - narration is best-effort, never fatal
            pass
    return template_summary(findings, as_of)
