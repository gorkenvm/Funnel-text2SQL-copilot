"""Unit tests for the module M6 display-only SQL pretty-printer."""

from __future__ import annotations

from agent.sqlfmt import format_sql_for_display


def test_uppercases_keywords():
    out = format_sql_for_display("select a, b from web_events where a = 1")
    assert "SELECT" in out
    assert "FROM" in out
    assert "WHERE" in out
    # identifiers are left alone (identifier_case=None)
    assert "web_events" in out


def test_reindents_a_one_line_query_onto_multiple_lines():
    out = format_sql_for_display(
        "SELECT device_category, count(*) AS n FROM web_events GROUP BY device_category"
    )
    assert "\n" in out
    assert out.startswith("SELECT device_category")
    assert "FROM web_events" in out
    assert "GROUP BY device_category" in out


def test_passthrough_on_none():
    assert format_sql_for_display(None) is None


def test_passthrough_on_empty_string():
    assert format_sql_for_display("") == ""


def test_passthrough_on_garbage_sql():
    garbage = "this is not ; sql at ((( all !!"
    # sqlparse never raises on arbitrary text; it just echoes it back
    # (possibly with cosmetic whitespace changes) rather than crashing —
    # prove the helper is safe on non-SQL input either way.
    out = format_sql_for_display(garbage)
    assert isinstance(out, str)
    assert out.strip() != "" or garbage.strip() == ""


def test_passthrough_when_sqlparse_missing(monkeypatch):
    """Simulate the optional dependency being absent: input comes back untouched."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sqlparse":
            raise ImportError("simulated missing dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    sql = "select 1"
    assert format_sql_for_display(sql) == sql


def test_never_raises_on_formatting_exception(monkeypatch):
    """Any exception from sqlparse.format degrades to returning the input."""
    import sqlparse

    def boom(*args, **kwargs):
        raise RuntimeError("simulated formatter crash")

    monkeypatch.setattr(sqlparse, "format", boom)
    sql = "select 1"
    assert format_sql_for_display(sql) == sql
