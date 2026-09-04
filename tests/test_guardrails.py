"""Guardrail unit tests: reject the bad, accept the registry."""

from __future__ import annotations

import pytest

from agent.guardrails import GuardrailError, enforce_limit, validate_sql


class TestRejections:
    def test_rejects_drop(self):
        with pytest.raises(GuardrailError):
            validate_sql("DROP TABLE web_events")

    def test_rejects_ddl_hidden_in_select(self):
        with pytest.raises(GuardrailError):
            validate_sql("SELECT 1; DROP TABLE web_events")

    def test_rejects_multi_statement(self):
        with pytest.raises(GuardrailError):
            validate_sql(
                "SELECT count(*) FROM web_events; SELECT count(*) FROM app_events"
            )

    def test_rejects_unknown_table(self):
        with pytest.raises(GuardrailError) as excinfo:
            validate_sql("SELECT * FROM secret_pii_table")
        assert "secret_pii_table" in str(excinfo.value)

    def test_rejects_ground_truth_table(self):
        with pytest.raises(GuardrailError):
            validate_sql("SELECT * FROM _ground_truth")

    def test_rejects_unknown_table_in_join(self):
        with pytest.raises(GuardrailError):
            validate_sql(
                "SELECT * FROM web_events w JOIN customers c ON c.id = w.user_pseudo_id"
            )

    def test_rejects_unknown_table_in_comma_list(self):
        with pytest.raises(GuardrailError):
            validate_sql("SELECT * FROM web_events, hidden_table")

    def test_rejects_insert_update_delete(self):
        for sql in (
            "INSERT INTO web_events VALUES (1)",
            "UPDATE web_events SET country = 'DE'",
            "DELETE FROM web_events",
        ):
            with pytest.raises(GuardrailError):
                validate_sql(sql)

    def test_rejects_pragma_attach_copy(self):
        for sql in (
            "PRAGMA database_list",
            "ATTACH 'evil.db'",
            "COPY web_events TO 'out.csv'",
        ):
            with pytest.raises(GuardrailError):
                validate_sql(sql)

    def test_rejects_non_select_start(self):
        with pytest.raises(GuardrailError):
            validate_sql("EXPLAIN SELECT * FROM web_events")

    def test_rejects_file_reading_functions(self):
        with pytest.raises(GuardrailError):
            validate_sql("SELECT * FROM read_parquet('data/_ground_truth.parquet')")

    def test_rejects_quoted_path_as_table(self):
        with pytest.raises(GuardrailError):
            validate_sql("SELECT * FROM 'data/_ground_truth.parquet'")

    def test_rejects_empty(self):
        with pytest.raises(GuardrailError):
            validate_sql("   ")


class TestAcceptance:
    def test_accepts_simple_select(self):
        validate_sql("SELECT country, count(*) FROM web_events GROUP BY country")

    def test_accepts_cte_and_joins(self):
        validate_sql(
            """
            WITH opens AS (
              SELECT hashed_device_id, MIN(event_timestamp) AS ts
              FROM app_events WHERE event_name = 'app_open'
              GROUP BY hashed_device_id
            )
            SELECT b.market, count(*)
            FROM id_bridge b JOIN opens o ON o.hashed_device_id = b.app_device_id
            GROUP BY b.market
            """
        )

    def test_accepts_trailing_semicolon(self):
        validate_sql("SELECT count(*) FROM app_events;")

    def test_accepts_every_registry_query(self, registry):
        for key, metric in registry.items():
            validate_sql(metric["sql"])  # must not raise

    @pytest.mark.parametrize(
        "table",
        ["gold.attribution_first_vs_last", "gold.completion_by_channel_device"],
    )
    def test_accepts_the_new_m4a_qualified_gold_names(self, table):
        validate_sql(f"SELECT * FROM {table}")


class TestEnforceLimit:
    def test_appends_limit_when_absent(self):
        out = enforce_limit("SELECT * FROM web_events")
        assert out.endswith("LIMIT 5000")

    def test_respects_existing_limit(self):
        sql = "SELECT * FROM web_events LIMIT 10"
        assert enforce_limit(sql) == sql

    def test_custom_max_rows(self):
        out = enforce_limit("SELECT * FROM app_events", max_rows=7)
        assert out.endswith("LIMIT 7")

    def test_strips_trailing_semicolon(self):
        out = enforce_limit("SELECT * FROM app_events;")
        assert ";" not in out


class TestSqlglotASTGuardrails:
    """Module M7a: sqlglot AST-based validation is now the primary layer,
    with the original regex/token checks kept running afterwards as cheap
    defense-in-depth (see agent.guardrails module docstring). These cases
    specifically exercise obfuscations a flat token scanner alone could
    miss, plus the new LIMIT-capping behaviour.
    """

    def test_rejects_comment_obfuscated_table_name(self):
        with pytest.raises(GuardrailError) as excinfo:
            validate_sql("SELECT * FROM/**/secret_tbl")
        assert "secret_tbl" in str(excinfo.value)

    def test_accepts_double_quoted_known_table(self):
        # ANSI-quoted identifier naming a whitelisted table must be
        # accepted, not blanket-rejected as "a quoted table reference".
        validate_sql('SELECT count(*) FROM "web_events"')

    def test_accepts_backtick_quoted_known_table(self):
        validate_sql("SELECT count(*) FROM `web_events`")

    def test_rejects_unknown_table_inside_subquery(self):
        with pytest.raises(GuardrailError) as excinfo:
            validate_sql("SELECT * FROM (SELECT * FROM secret_tbl) t")
        assert "secret_tbl" in str(excinfo.value)

    def test_rejects_unknown_table_inside_join(self):
        with pytest.raises(GuardrailError) as excinfo:
            validate_sql(
                "SELECT * FROM web_events w "
                "JOIN (SELECT * FROM secret_tbl) s ON true"
            )
        assert "secret_tbl" in str(excinfo.value)

    def test_accepts_with_cte_wrapped_select(self):
        validate_sql(
            "WITH recent AS (SELECT * FROM web_events) "
            "SELECT count(*) FROM recent"
        )

    def test_rejects_insert(self):
        with pytest.raises(GuardrailError):
            validate_sql("INSERT INTO web_events (event_name) VALUES ('x')")

    def test_rejects_insert_hidden_in_cte_with_returning(self):
        # A single top-level SELECT whose CTE body is actually a write —
        # exactly the case that requires walking the *whole* AST, not
        # just checking the statement's root node type.
        with pytest.raises(GuardrailError):
            validate_sql(
                "WITH t AS (INSERT INTO web_events (event_name) "
                "VALUES ('x') RETURNING *) SELECT * FROM t"
            )

    def test_rejects_multi_statement_select_then_drop(self):
        with pytest.raises(GuardrailError):
            validate_sql("SELECT 1; DROP TABLE web_events")

    def test_enforce_limit_preserves_lower_existing_limit(self):
        sql = "SELECT * FROM web_events LIMIT 10"
        assert enforce_limit(sql, max_rows=5000) == sql

    def test_enforce_limit_caps_oversized_existing_limit(self):
        out = enforce_limit("SELECT * FROM web_events LIMIT 999999", max_rows=5000)
        assert out == "SELECT * FROM web_events LIMIT 5000"


class TestSqlglotFallback:
    """The legacy-only fallback path (sqlglot not importable) must behave
    identically to the pre-M7a guardrails — proven by monkeypatching the
    lazy sqlglot import to fail, the same way app code would degrade on a
    machine without the optional dependency installed."""

    def test_legacy_only_still_accepts_known_tables(self, monkeypatch):
        import agent.guardrails as guardrails

        monkeypatch.setattr(guardrails, "_import_sqlglot", lambda: (None, None))
        validate_sql("SELECT count(*) FROM web_events")  # must not raise

    def test_legacy_only_still_rejects_unknown_tables(self, monkeypatch):
        import agent.guardrails as guardrails

        monkeypatch.setattr(guardrails, "_import_sqlglot", lambda: (None, None))
        with pytest.raises(GuardrailError):
            validate_sql("SELECT * FROM secret_pii_table")

    def test_legacy_only_still_rejects_insert(self, monkeypatch):
        import agent.guardrails as guardrails

        monkeypatch.setattr(guardrails, "_import_sqlglot", lambda: (None, None))
        with pytest.raises(GuardrailError):
            validate_sql("INSERT INTO web_events VALUES (1)")
