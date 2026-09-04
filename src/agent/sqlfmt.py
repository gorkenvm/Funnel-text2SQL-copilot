"""Best-effort SQL pretty-printing for **display only** (module M6).

:func:`format_sql_for_display` is applied ONLY to the copy of a SQL string
shown in the UI's "show SQL" panel — never to the SQL string that is
actually validated, guardrailed or executed against the driver. Callers
must always run/guard the original string first and format a separate
copy at the last moment, right before it goes into a response payload or
trace event.

The helper is intentionally defensive: a missing ``sqlparse`` dependency,
a ``None``/non-string input, or any formatting exception all fall back to
returning the input unchanged, so a display nicety can never break an
API response.
"""

from __future__ import annotations

from typing import Optional


def format_sql_for_display(sql: Optional[str]) -> Optional[str]:
    """Reindent and upper-case keywords in ``sql`` for on-screen display.

    Returns ``sql`` unchanged (including ``None`` or a non-string value)
    whenever ``sqlparse`` is not installed or formatting fails for any
    reason — this function must never raise.
    """
    if not isinstance(sql, str) or not sql.strip():
        return sql
    try:
        import sqlparse  # lazy import: keep the dependency optional at import time
    except ImportError:  # pragma: no cover - exercised only if sqlparse is absent
        return sql
    try:
        formatted = sqlparse.format(
            sql,
            reindent=True,
            keyword_case="upper",
            identifier_case=None,
            strip_comments=False,
        )
    except Exception:  # noqa: BLE001 - a display nicety must never break the response
        return sql
    return formatted if formatted and formatted.strip() else sql
