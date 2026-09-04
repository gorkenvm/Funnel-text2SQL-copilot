"""SQL guardrails for the ask-the-funnel agent.

Every LLM-generated SQL statement passes through :func:`validate_sql`
before it touches a database, and through :func:`enforce_limit` so that
no query can return an unbounded result set.

Since module M7a, validation is a **two-layer** pipeline:

1. **Primary — sqlglot AST validation** (:func:`_validate_sql_via_sqlglot`).
   The statement is parsed into a real SQL abstract syntax tree (trying
   the ``databricks`` dialect, then ``duckdb``, then a dialect-less
   generic parse — the query is accepted the moment any of the three
   succeeds), which lets us reason about *structure* rather than tokens:
   exactly one statement; that statement must be a read-only query
   (``SELECT``, optionally ``WITH``-wrapped, or a set operation over
   ``SELECT``s); no DDL/DML/engine-control node may appear *anywhere* in
   the tree (this catches sneaky constructs a flat token scanner would
   miss, e.g. ``WITH t AS (INSERT ... RETURNING *) SELECT * FROM t``);
   and every table reference anywhere in the tree — including inside
   CTEs, subqueries and joins, but excluding the query's own CTE aliases
   — must resolve to the whitelist. Because it is a real parser, this
   layer is naturally immune to comment/whitespace obfuscation between
   keywords and correctly reads quoted identifiers as identifiers (not
   as suspicious punctuation).
2. **Secondary — the original regex/token checks** (kept, cheap, and
   run as defense-in-depth on every call, whether or not sqlglot ran):
   exactly one statement, read-only (``SELECT``/``WITH`` only); no DDL/
   DML or engine-control keywords anywhere; no file-reading table
   functions (``read_parquet`` & friends); only whitelisted tables (CTE
   names defined inside the query itself are allowed too).

If ``sqlglot`` is not importable, validation silently falls back to the
legacy layer alone (a warning is logged once) — behaviour identical to
the pre-M7a guardrails.

The table whitelist covers both the legacy bare table names and the
fully-qualified bronze/silver/gold medallion objects (see
agent.medallion).
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

from agent import medallion

logger = logging.getLogger(__name__)

#: Legacy bare table names (still resolvable unqualified against the raw
#: parquet views on DuckDB, or the connection's default schema/catalog on
#: Databricks) — kept whitelisted for backward compatibility with any SQL
#: written against the pre-medallion (M3b) surface.
_LEGACY_TABLES: frozenset[str] = frozenset({"web_events", "app_events", "id_bridge"})


def _qualified(layer: str, names: tuple[str, ...]) -> frozenset[str]:
    return frozenset(f"{layer}.{name}" for name in names)


#: Medallion (M3c) layered objects the agent may reference, fully qualified
#: as ``<layer>.<object>``. Deliberately an EXACT list, not a wildcard on
#: ``bronze.*``/``silver.*``/``gold.*`` — built from agent.medallion's
#: canonical BRONZE_TABLES/SILVER_TABLES/GOLD_TABLES so this whitelist can
#: never silently drift from what sql/medallion.sql actually creates. See
#: sql/medallion.sql for what each object holds.
_BRONZE_TABLES: frozenset[str] = _qualified("bronze", medallion.BRONZE_TABLES)
_SILVER_TABLES: frozenset[str] = _qualified("silver", medallion.SILVER_TABLES)
_GOLD_TABLES: frozenset[str] = _qualified("gold", medallion.GOLD_TABLES)

#: Tables an agent query may reference: the legacy bare names plus every
#: qualified bronze/silver/gold object (exact list — see above).
ALLOWED_TABLES: frozenset[str] = (
    _LEGACY_TABLES | _BRONZE_TABLES | _SILVER_TABLES | _GOLD_TABLES
)

#: Statement keywords that are never allowed inside an agent query.
FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "create",
        "attach",
        "copy",
        "pragma",
        "call",
        "merge",
        "grant",
        "revoke",
        "truncate",
        "install",
        "load",
        "export",
        "import",
        "set",
        "vacuum",
    }
)

#: Table functions that would bypass the table whitelist.
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "read_parquet",
        "read_csv",
        "read_csv_auto",
        "read_json",
        "read_json_auto",
        "parquet_scan",
        "glob",
        "sniff_csv",
    }
)

#: SQL keywords that terminate the FROM-clause context in the mini parser.
_FROM_TERMINATORS: frozenset[str] = frozenset(
    {
        "select",
        "where",
        "group",
        "order",
        "having",
        "window",
        "limit",
        "qualify",
        "union",
        "intersect",
        "except",
        "on",
        "using",
        "when",
        "then",
        "else",
        "end",
        "case",
        "and",
        "or",
        "not",
        "with",
        "as",
    }
)

_JOIN_MODIFIERS: frozenset[str] = frozenset(
    {"inner", "left", "right", "full", "outer", "cross", "natural", "semi", "anti", "lateral"}
)

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_CTE_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\.]*|[(),;\"'`]|\S")
_LIMIT_RE = re.compile(r"\blimit\s+\d+", re.IGNORECASE)

#: sqlglot read dialects to try, in order — the query is accepted as soon
#: as any one of them parses it. ``None`` is sqlglot's dialect-less
#: generic SQL parser, tried last as a catch-all.
_SQLGLOT_DIALECTS: tuple[Optional[str], ...] = ("databricks", "duckdb", None)

#: AST node type names that must never appear anywhere in a validated
#: query's tree — DDL, DML, and every engine-control statement the legacy
#: FORBIDDEN_KEYWORDS list also names. Looked up by name against the
#: installed sqlglot's `expressions` module so this file never hard-fails
#: if a given sqlglot version renames/removes one of the rarer ones.
_FORBIDDEN_AST_NODE_NAMES: tuple[str, ...] = (
    "Insert",
    "Update",
    "Delete",
    "Drop",
    "Alter",
    "Create",
    "Command",
    "Merge",
    "Grant",
    "Revoke",
    "TruncateTable",
    "Pragma",
    "Attach",
    "Copy",
    "Set",
    "Install",
    "LoadData",
    "Use",
    "Cache",
    "Uncache",
    "Export",
)

#: AST node types a validated statement's ROOT may be — a plain SELECT
#: (CTEs live inside it, not as a separate wrapper node in sqlglot), or a
#: set operation combining SELECTs (UNION/INTERSECT/EXCEPT), all of which
#: are read-only by construction.
_READ_QUERY_NODE_NAMES: tuple[str, ...] = ("Select", "Union", "Intersect", "Except")

#: Cached across calls: sqlglot logs a warning about its own absence only
#: once per process, not once per validate_sql() call.
_warned_missing_sqlglot = False


class GuardrailError(Exception):
    """Raised when a query violates the agent's safety rules."""


# ---------------------------------------------------------------------------
# sqlglot AST layer (primary)
# ---------------------------------------------------------------------------
def _import_sqlglot():
    """Lazy, best-effort import of sqlglot; ``(None, None)`` if unavailable."""
    try:
        import sqlglot  # noqa: PLC0415 - intentionally lazy
        from sqlglot import expressions as exp  # noqa: PLC0415
    except ImportError:
        return None, None
    return sqlglot, exp


def _warn_sqlglot_missing_once() -> None:
    global _warned_missing_sqlglot
    if not _warned_missing_sqlglot:
        logger.warning(
            "sqlglot is not installed; SQL guardrails are running on the "
            "legacy regex/token checks only (identical to pre-M7a "
            "behaviour). Run `pip install sqlglot` to enable the "
            "AST-based primary validation layer."
        )
        _warned_missing_sqlglot = True


def _parse_any_dialect(sqlglot_mod, body: str) -> list:
    """Parse ``body`` with the first dialect in :data:`_SQLGLOT_DIALECTS`
    that succeeds; returns the list of parsed statements (``None`` entries
    from stray trailing semicolons filtered out).

    Raises :class:`GuardrailError` (a friendly, user-facing message) when
    *no* dialect can parse the text at all.
    """
    last_exc: Optional[Exception] = None
    for dialect in _SQLGLOT_DIALECTS:
        try:
            parsed = [s for s in sqlglot_mod.parse(body, read=dialect) if s is not None]
        except Exception as exc:  # noqa: BLE001 - sqlglot raises its own ParseError types
            last_exc = exc
            continue
        return parsed
    raise GuardrailError(
        "I could not safely parse that SQL. Please check the syntax and "
        "try again."
    ) from last_exc


def _table_names_from_ast(statement, exp_mod, cte_aliases: frozenset[str]) -> list[str]:
    """Every table reference in ``statement``'s tree, qualified + lowered.

    Walks the *entire* AST (``find_all`` recurses into CTEs, subqueries
    and joins), so a table hidden three levels deep in a subquery is just
    as visible as one in the outermost FROM clause. A reference whose bare
    name matches one of the query's own CTE aliases is skipped — that is
    the query referencing its own named step, not an external table.
    """
    names: list[str] = []
    for table in statement.find_all(exp_mod.Table):
        raw_name = table.name
        if not raw_name:
            # A table-valued function (e.g. read_parquet(...)) has no
            # plain identifier name — render it so it still shows up as a
            # clearly-unknown "table" in the rejection message rather than
            # silently vanishing as an empty string.
            names.append(table.sql().strip().lower())
            continue
        if not table.db and raw_name.lower() in cte_aliases:
            continue
        qualified = f"{table.db}.{raw_name}".lower() if table.db else raw_name.lower()
        names.append(qualified)
    return names


def _validate_sql_via_sqlglot(sql: str, sqlglot_mod, exp_mod) -> None:
    body = sql.strip().rstrip(";").strip()
    statements = _parse_any_dialect(sqlglot_mod, body)

    if len(statements) != 1:
        raise GuardrailError(
            "Multiple SQL statements are not allowed — please send a single "
            "SELECT query."
        )
    statement = statements[0]

    read_types = tuple(
        getattr(exp_mod, name) for name in _READ_QUERY_NODE_NAMES if hasattr(exp_mod, name)
    )
    if not isinstance(statement, read_types):
        raise GuardrailError(
            "Only read-only queries are allowed: the statement must start "
            "with SELECT or WITH."
        )

    forbidden_types = tuple(
        getattr(exp_mod, name) for name in _FORBIDDEN_AST_NODE_NAMES if hasattr(exp_mod, name)
    )
    forbidden_nodes = list(statement.find_all(forbidden_types)) if forbidden_types else []
    if forbidden_nodes:
        kinds = sorted({type(node).__name__.upper() for node in forbidden_nodes})
        raise GuardrailError(
            "This query contains operations I'm not allowed to run "
            f"({', '.join(kinds)}). I can only read data with SELECT "
            "queries."
        )

    cte_aliases = frozenset(
        cte.alias.lower() for cte in statement.find_all(exp_mod.CTE) if cte.alias
    )
    unknown = sorted(
        {
            name
            for name in _table_names_from_ast(statement, exp_mod, cte_aliases)
            if name not in ALLOWED_TABLES
        }
    )
    if unknown:
        raise GuardrailError(
            f"I can only query these tables: {', '.join(sorted(ALLOWED_TABLES))}. "
            f"Unknown table(s) referenced: {', '.join(unknown)}."
        )


def extract_referenced_tables(sql: Optional[str]) -> list[str]:
    """Best-effort, non-raising list of tables ``sql`` references.

    Used by :mod:`agent.memory` (module M7a) to record which tables a
    conversational turn touched, without duplicating table-extraction
    logic. This is purely informational — never a security boundary
    (:func:`validate_sql` is) — so it never raises: unparseable or empty
    input just yields an empty list.
    """
    if not sql or not sql.strip():
        return []
    sqlglot_mod, exp_mod = _import_sqlglot()
    if sqlglot_mod is not None:
        body = sql.strip().rstrip(";").strip()
        for dialect in _SQLGLOT_DIALECTS:
            try:
                statement = sqlglot_mod.parse_one(body, read=dialect)
            except Exception:  # noqa: BLE001
                continue
            try:
                cte_aliases = frozenset(
                    cte.alias.lower() for cte in statement.find_all(exp_mod.CTE) if cte.alias
                )
                names = _table_names_from_ast(statement, exp_mod, cte_aliases)
                return sorted({n for n in names if n})
            except Exception:  # noqa: BLE001 - informational helper, never raises
                return []
    # Legacy fallback (no sqlglot, or every dialect failed to parse).
    try:
        cleaned = _strip_comments_and_strings(sql)
        body = cleaned.rstrip(";").strip()
        cte_names = {m.group(1).lower() for m in _CTE_RE.finditer(body)}
        return sorted({name for name in _referenced_tables(body) if name not in cte_names})
    except GuardrailError:
        return []


# ---------------------------------------------------------------------------
# Legacy regex/token layer (secondary — defense-in-depth, and the sole
# layer used when sqlglot is not installed).
# ---------------------------------------------------------------------------
def _strip_comments_and_strings(sql: str) -> str:
    """Replace comments and string literals with spaces (keeps offsets sane)."""
    no_comments = _COMMENT_RE.sub(" ", sql)
    return _STRING_RE.sub(" '' ", no_comments)


def _referenced_tables(cleaned_sql: str) -> Iterable[str]:
    """Yield table names referenced in FROM / JOIN clauses.

    A small token state machine: after ``FROM`` or ``JOIN`` (and after a
    comma at the same paren depth while still inside a FROM list) the next
    identifier is a table reference. Sub-selects, aliases and join
    modifiers are skipped. A double-quoted or backtick-quoted identifier
    (e.g. ``"web_events"``) is read as the table name it names — that is
    legitimate ANSI/Databricks identifier quoting, not an attempt to sneak
    in a file path; a *single*-quoted string in table position (a real
    string literal, e.g. ``FROM 'data/x.parquet'``) is still rejected.
    """
    tokens = _TOKEN_RE.findall(cleaned_sql)
    n = len(tokens)
    depth = 0
    expecting_table = False
    from_depth: int | None = None  # paren depth of the active FROM list
    i = 0

    while i < n:
        token = tokens[i]
        lowered = token.lower()

        if token == "(":
            if expecting_table:  # derived table: FROM ( SELECT ... )
                expecting_table = False
            depth += 1
            i += 1
            continue
        if token == ")":
            depth -= 1
            if from_depth is not None and depth < from_depth:
                from_depth = None
            i += 1
            continue
        if token == ",":
            if from_depth is not None and depth == from_depth:
                expecting_table = True
            i += 1
            continue
        if token in ('"', "`"):
            if not expecting_table:
                i += 1
                continue
            close = token
            j = i + 1
            parts: list[str] = []
            while j < n and tokens[j] != close:
                parts.append(tokens[j])
                j += 1
            if j >= n:
                raise GuardrailError(
                    "Unterminated quoted identifier in a FROM/JOIN clause."
                )
            name = "".join(parts).strip().lower()
            expecting_table = False
            i = j + 1
            if name:
                yield name
            continue
        if token == "'":
            if expecting_table:
                raise GuardrailError(
                    "Quoted or file-path table references are not allowed. "
                    "Please query only web_events, app_events or id_bridge."
                )
            i += 1
            continue
        if lowered in {"from", "join"}:
            expecting_table = True
            from_depth = depth
            i += 1
            continue
        if lowered in _JOIN_MODIFIERS:
            i += 1
            continue
        if lowered in _FROM_TERMINATORS:
            expecting_table = False
            # Keywords that may appear inside a join condition keep the FROM
            # list open; clause-starting keywords close it (so that commas in
            # e.g. GROUP BY lists are not mistaken for cross joins).
            if lowered not in {"on", "using", "as", "and", "or", "not", "case", "when", "then", "else", "end"}:
                from_depth = None
            i += 1
            continue
        if expecting_table and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\.]*", token):
            expecting_table = False
            yield lowered
            i += 1
            continue
        # any other identifier while not expecting a table: alias/column, skip
        i += 1


def _validate_sql_legacy(sql: str) -> None:
    """The original (pre-M7a) regex/token guardrail checks, unchanged."""
    cleaned = _strip_comments_and_strings(sql).strip()
    body = cleaned.rstrip(";").strip()

    if ";" in body:
        raise GuardrailError(
            "Multiple SQL statements are not allowed — please send a single "
            "SELECT query."
        )

    first_word = re.match(r"[A-Za-z]+", body)
    if not first_word or first_word.group(0).lower() not in {"select", "with"}:
        raise GuardrailError(
            "Only read-only queries are allowed: the statement must start "
            "with SELECT or WITH."
        )

    words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body)}
    forbidden = sorted(words & FORBIDDEN_KEYWORDS)
    if forbidden:
        raise GuardrailError(
            "This query contains keywords I'm not allowed to run "
            f"({', '.join(k.upper() for k in forbidden)}). I can only read "
            "data with SELECT queries."
        )
    dangerous_fns = sorted(words & FORBIDDEN_FUNCTIONS)
    if dangerous_fns:
        raise GuardrailError(
            "Direct file access functions are not allowed "
            f"({', '.join(dangerous_fns)}). Please query the registered "
            "tables instead."
        )

    cte_names = {m.group(1).lower() for m in _CTE_RE.finditer(body)}
    allowed = ALLOWED_TABLES | cte_names
    unknown = sorted(
        {name for name in _referenced_tables(body) if name not in allowed}
    )
    if unknown:
        raise GuardrailError(
            f"I can only query these tables: {', '.join(sorted(ALLOWED_TABLES))}. "
            f"Unknown table(s) referenced: {', '.join(unknown)}."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def validate_sql(sql: str) -> None:
    """Validate an agent SQL statement; raise :class:`GuardrailError` if unsafe.

    Primary layer: parse ``sql`` into a real AST with sqlglot and check
    its *structure* (single read-only statement, no DDL/DML/command node
    anywhere in the tree, every referenced table whitelisted). Secondary
    layer: the original regex/token checks, always run afterwards as
    cheap defense-in-depth. Falls back to the secondary layer alone (with
    a logged warning) if sqlglot is not installed.
    """
    if not sql or not sql.strip():
        raise GuardrailError("I received an empty SQL statement, so I can't run it.")

    sqlglot_mod, exp_mod = _import_sqlglot()
    if sqlglot_mod is not None:
        _validate_sql_via_sqlglot(sql, sqlglot_mod, exp_mod)
    else:
        _warn_sqlglot_missing_once()

    _validate_sql_legacy(sql)


def _find_real_limit_matches(body: str) -> list[re.Match]:
    """``LIMIT <n>`` occurrences in ``body`` that are real SQL, not text
    that merely looks like one inside a comment or a string literal.

    Matched against the *original* text (so match offsets are valid for
    splicing back into ``body`` itself, unlike the comment/string-stripped
    copy used elsewhere), with any match falling inside a comment or
    string-literal span discarded.
    """
    excluded_spans = [m.span() for m in _COMMENT_RE.finditer(body)] + [
        m.span() for m in _STRING_RE.finditer(body)
    ]
    return [
        m
        for m in _LIMIT_RE.finditer(body)
        if not any(start <= m.start() < end for start, end in excluded_spans)
    ]


def _existing_limit_value(limit_node) -> Optional[int]:
    """Best-effort integer value of a sqlglot LIMIT node's expression."""
    expression = getattr(limit_node, "expression", None)
    value = getattr(expression, "this", None)
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _existing_limit_value_via_sqlglot(body: str) -> Optional[int]:
    """The outer statement's LIMIT value via sqlglot, or ``None`` when it
    cannot be confidently determined (sqlglot unavailable, every dialect
    failed to parse, no LIMIT node, or a non-integer LIMIT expression)."""
    sqlglot_mod, _exp_mod = _import_sqlglot()
    if sqlglot_mod is None:
        return None
    for dialect in _SQLGLOT_DIALECTS:
        try:
            statement = sqlglot_mod.parse_one(body, read=dialect)
        except Exception:  # noqa: BLE001
            continue
        limit_node = statement.args.get("limit")
        if limit_node is None:
            return None
        return _existing_limit_value(limit_node)
    return None


def enforce_limit(sql: str, max_rows: int = 5000) -> str:
    """Return ``sql`` with a ``LIMIT`` added, or capped at ``max_rows``.

    An absent LIMIT is appended (string-append, exactly as before M7a).
    An existing LIMIT already at or below ``max_rows`` is left completely
    untouched — including its exact original text/casing. An existing
    LIMIT sqlglot can confidently read as a plain integer *above*
    ``max_rows`` is capped down via a minimal, surgical text splice of
    just that number (the rest of the query — casing, whitespace,
    everything — is left byte-for-byte unchanged); this is the one case
    that needs sqlglot's structural understanding of "which LIMIT belongs
    to the outer query" — the legacy layer has no such capping behaviour
    at all. Whenever the existing value cannot be confidently read as a
    plain integer (sqlglot unavailable, unparseable, or a non-literal
    LIMIT expression), the clause is respected verbatim — the same
    conservative choice the pre-M7a code always made.
    """
    body = sql.strip().rstrip(";").strip()

    limit_matches = _find_real_limit_matches(body)
    if not limit_matches:
        return f"{body}\nLIMIT {max_rows}"

    existing_n = _existing_limit_value_via_sqlglot(body)
    if existing_n is not None and existing_n > max_rows:
        last = limit_matches[-1]
        return f"{body[: last.start()]}LIMIT {max_rows}{body[last.end() :]}"

    return body
