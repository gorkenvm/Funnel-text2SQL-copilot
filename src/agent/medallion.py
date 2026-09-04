"""Applier for ``sql/medallion.sql`` — the single, versioned source of truth
for the bronze/silver/gold lakehouse layers.

Both engines run the exact same statements, in the exact same order,
through :func:`apply_medallion`:

* :class:`agent.db.DuckDBDriver` calls it at startup, in-memory, with
  ``raw_schema="main"`` (where the three parquet files are registered as
  views).
* ``scripts/load_to_databricks.py`` calls it after loading the raw parquet
  files into a staging schema, with ``raw_schema=<that schema>``.

The file uses exactly one template variable, ``{{raw}}``, substituted with
``raw_schema`` before any statement runs — see the header comment of
``sql/medallion.sql`` for the full templating contract.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

#: Location of the single source-of-truth SQL file (repo_root/sql/medallion.sql).
MEDALLION_SQL_PATH = Path(__file__).resolve().parents[2] / "sql" / "medallion.sql"

#: The one substitution token the SQL file may contain.
_RAW_TOKEN = "{{raw}}"

#: Canonical inventory of every object sql/medallion.sql creates, unqualified
#: (bare object name — the caller prefixes with "bronze."/"silver."/"gold.").
#: This is the single place that inventory is spelled out in Python; both
#: agent.guardrails (query whitelist) and scripts/load_to_databricks.py
#: (verification printout) import it rather than re-deriving it, so the SQL
#: file and its consumers cannot silently drift apart.
BRONZE_TABLES: tuple[str, ...] = ("web_events", "app_events", "id_bridge")

SILVER_TABLES: tuple[str, ...] = (
    "web_user_stages",
    "app_user_stages",
    "v_attribution_eligible",
    "linked_journeys",
)

GOLD_TABLES: tuple[str, ...] = (
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
    # M11: dimensional, filterable gold cubes backing the natural-language
    # "build me a KPI dashboard for <range>/<market>" flow — see
    # config/dashboard_kpis.json and agent.dashboard. DAY-grained since the
    # M11-fix (was week-grained; a day-level filter like "last 3 days" is
    # structurally unanswerable off a week_start cube — see sql/medallion.sql).
    "web_funnel_daily_cube",
    "journey_daily_cube",
)


def _split_on_unquoted_semicolons(body: str) -> list[str]:
    """Split ``body`` on ``;`` that are NOT inside a single-quoted string.

    A plain ``str.split(";")`` would also split on a literal semicolon
    that happens to appear as English punctuation inside a ``COMMENT ON
    ... IS '...'`` string (several do, e.g. "...population; use..."). This
    walks the text tracking single-quoted-string state (with the standard
    SQL ``''`` escape for a literal quote) and only splits outside one.
    """
    statements: list[str] = []
    current: list[str] = []
    in_string = False
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if in_string:
            if ch == "'":
                if i + 1 < n and body[i + 1] == "'":  # escaped '' inside a string
                    current.append("''")
                    i += 2
                    continue
                in_string = False
            current.append(ch)
        else:
            if ch == "'":
                in_string = True
                current.append(ch)
            elif ch == ";":
                statements.append("".join(current))
                current = []
            else:
                current.append(ch)
        i += 1
    tail = "".join(current)
    if tail.strip():
        statements.append(tail)
    return statements


def parse_statements(sql_text: str) -> list[str]:
    """Split ``sql_text`` into individual statements, in file order.

    Lines whose first non-whitespace characters are ``--`` are dropped as
    full-line comments (the only comment style ``medallion.sql`` uses); the
    remainder is joined back and split on ``;`` that fall outside any
    single-quoted string literal (see :func:`_split_on_unquoted_semicolons`
    — a ``COMMENT ON`` string may itself contain a literal ``;``).
    Empty/whitespace-only statements are discarded.
    """
    kept_lines = [
        line for line in sql_text.splitlines() if not line.lstrip().startswith("--")
    ]
    body = "\n".join(kept_lines)
    statements = [stmt.strip() for stmt in _split_on_unquoted_semicolons(body)]
    return [stmt for stmt in statements if stmt]


def render_statements(raw_schema: str) -> list[str]:
    """Read ``sql/medallion.sql``, substitute ``{{raw}}``, and return statements."""
    sql_text = MEDALLION_SQL_PATH.read_text(encoding="utf-8")
    statements = parse_statements(sql_text)
    return [stmt.replace(_RAW_TOKEN, raw_schema) for stmt in statements]


def apply_medallion(execute_fn: Callable[[str], object], raw_schema: str) -> list[str]:
    """Apply the medallion SQL, in order, via ``execute_fn``.

    Parameters
    ----------
    execute_fn:
        A callable that runs one SQL statement against the target engine
        (e.g. a DuckDB connection's ``execute``, or a Databricks cursor's
        ``execute``). Called once per statement, in file order.
    raw_schema:
        The value substituted for ``{{raw}}`` — the schema holding the raw
        ``web_events`` / ``app_events`` / ``id_bridge`` tables before this
        file runs (``"main"`` for DuckDB, the load schema for Databricks).

    Returns
    -------
    The list of statements that were actually executed (post-substitution),
    for logging/tests.

    A ``COMMENT ON ...`` statement that fails is logged and skipped — some
    engine/version combinations may not support it, and documentation
    comments must never block the layers themselves from building. Any
    other statement's failure is fatal and re-raised.
    """
    statements = render_statements(raw_schema)
    for statement in statements:
        try:
            execute_fn(statement)
        except Exception as exc:  # noqa: BLE001 - re-raised unless COMMENT ON
            if statement.lstrip().upper().startswith("COMMENT ON"):
                logger.warning(
                    "medallion: COMMENT ON statement failed, skipping (non-fatal): %s (%s)",
                    statement.splitlines()[0][:120],
                    exc,
                )
                continue
            raise
    return statements
