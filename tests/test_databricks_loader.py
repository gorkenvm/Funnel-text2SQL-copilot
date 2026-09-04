"""Offline tests for module M3b: the Databricks loader script and the
catalog/schema wiring in agent.db.DatabricksDriver.

Nothing here ever touches a real Databricks workspace: SQL-statement
generation and environment validation are pure-function tests, and the
"connection" used by run() and by DatabricksDriver is always either a
plain fake object or a fake module injected into sys.modules — never the
real databricks-sql-connector network path.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from agent.db import DEFAULT_DATABRICKS_CATALOG, DEFAULT_DATABRICKS_SCHEMA
from agent.medallion import GOLD_TABLES, render_statements

from scripts import load_to_databricks as loader

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


# ---------------------------------------------------------------------------
# Environment validation
# ---------------------------------------------------------------------------
class TestLoadSettings:
    def test_missing_all_required_vars_raises_with_every_name(self):
        with pytest.raises(loader.ConfigError) as exc_info:
            loader.load_settings(env={})
        message = str(exc_info.value)
        for var in loader.REQUIRED_ENV_VARS:
            assert var in message

    def test_missing_one_required_var_raises_naming_only_that_one(self):
        env = {
            "DATABRICKS_SERVER_HOSTNAME": "adb-1.azuredatabricks.net",
            "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc123",
            # DATABRICKS_TOKEN missing
        }
        with pytest.raises(loader.ConfigError) as exc_info:
            loader.load_settings(env=env)
        assert "DATABRICKS_TOKEN" in str(exc_info.value)
        assert "DATABRICKS_SERVER_HOSTNAME" not in str(exc_info.value)

    def test_defaults_catalog_and_schema_when_unset(self):
        env = {
            "DATABRICKS_SERVER_HOSTNAME": "adb-1.azuredatabricks.net",
            "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc123",
            "DATABRICKS_TOKEN": "dapi-fake",
        }
        settings = loader.load_settings(env=env)
        assert settings.catalog == DEFAULT_DATABRICKS_CATALOG == "workspace"
        assert settings.schema == DEFAULT_DATABRICKS_SCHEMA == "sonova"

    def test_explicit_catalog_and_schema_override_defaults(self):
        env = {
            "DATABRICKS_SERVER_HOSTNAME": "adb-1.azuredatabricks.net",
            "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc123",
            "DATABRICKS_TOKEN": "dapi-fake",
            "DATABRICKS_CATALOG": "main",
            "DATABRICKS_SCHEMA": "custom_schema",
        }
        settings = loader.load_settings(env=env)
        assert settings.catalog == "main"
        assert settings.schema == "custom_schema"


# ---------------------------------------------------------------------------
# SQL-statement generation
# ---------------------------------------------------------------------------
@pytest.fixture
def settings():
    return loader.Settings(
        server_hostname="adb-1.azuredatabricks.net",
        http_path="/sql/1.0/warehouses/abc123",
        token="dapi-fake",
        catalog="workspace",
        schema="sonova",
    )


class TestStatementGeneration:
    def test_schema_ddl(self, settings):
        assert loader.schema_ddl(settings) == "CREATE SCHEMA IF NOT EXISTS workspace.sonova"

    def test_volume_ddl(self, settings):
        assert (
            loader.volume_ddl(settings)
            == "CREATE VOLUME IF NOT EXISTS workspace.sonova.raw"
        )

    def test_volume_path(self, settings):
        assert (
            loader.volume_path(settings, "web_events")
            == "/Volumes/workspace/sonova/raw/web_events.parquet"
        )

    def test_put_statement_uses_overwrite_and_volume_path(self, settings, tmp_path):
        local = tmp_path / "web_events.parquet"
        local.write_bytes(b"")
        stmt = loader.put_statement(local, settings, "web_events")
        assert stmt.startswith("PUT ")
        assert stmt.endswith("OVERWRITE")
        assert "/Volumes/workspace/sonova/raw/web_events.parquet" in stmt
        assert local.as_posix() in stmt

    def test_ctas_statement_selects_from_volume_parquet(self, settings):
        stmt = loader.ctas_statement(settings, "app_events")
        assert stmt.startswith("CREATE OR REPLACE TABLE workspace.sonova.app_events AS")
        assert "parquet.`/Volumes/workspace/sonova/raw/app_events.parquet`" in stmt

    def test_count_statement_targets_fully_qualified_table(self, settings):
        stmt = loader.count_statement(settings, "id_bridge")
        assert stmt == "SELECT COUNT(*) AS n FROM workspace.sonova.id_bridge"

    def test_statements_are_idempotent_by_construction(self, settings):
        # CREATE ... IF NOT EXISTS / OVERWRITE / CREATE OR REPLACE are all
        # safe to run repeatedly — this just documents/locks that choice.
        assert "IF NOT EXISTS" in loader.schema_ddl(settings)
        assert "IF NOT EXISTS" in loader.volume_ddl(settings)
        assert "OVERWRITE" in loader.put_statement(Path("x.parquet"), settings, "web_events")
        assert "CREATE OR REPLACE" in loader.ctas_statement(settings, "web_events")


# ---------------------------------------------------------------------------
# Local (pyarrow) row counts
# ---------------------------------------------------------------------------
class TestLocalRowCounts:
    @pytest.mark.parametrize("table", list(loader.TABLE_NAMES))
    def test_local_row_count_matches_pyarrow_directly(self, table):
        expected = pq.read_table(DATA_DIR / f"{table}.parquet").num_rows
        assert loader.local_row_count(table, data_dir=DATA_DIR) == expected

    def test_ground_truth_is_never_in_table_names(self):
        assert "_ground_truth" not in loader.TABLE_NAMES

    def test_missing_local_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            loader.local_parquet_path("web_events", data_dir=tmp_path)


# ---------------------------------------------------------------------------
# run() against a mocked connection (no network)
# ---------------------------------------------------------------------------
class FakeCursor:
    """Records every executed statement; fetchone() replays `counts` in
    the order COUNT(*) statements are executed."""

    def __init__(self, counts):
        self.executed: list[str] = []
        self._counts = list(counts)
        self._idx = 0
        self.closed = False

    def execute(self, sql):
        self.executed.append(sql)
        if sql.startswith("SELECT COUNT(*)") and self._idx < len(self._counts):
            self._pending = self._counts[self._idx]
            self._idx += 1

    def fetchone(self):
        return (self._pending,)

    def close(self):
        self.closed = True


class FailingCursor(FakeCursor):
    def __init__(self, counts, fail_on_prefix):
        super().__init__(counts)
        self._fail_on_prefix = fail_on_prefix

    def execute(self, sql):
        if sql.startswith(self._fail_on_prefix):
            raise RuntimeError("simulated failure: PERMISSION_DENIED (403)")
        super().execute(sql)


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


@pytest.fixture
def local_counts():
    return [loader.local_row_count(t, data_dir=DATA_DIR) for t in loader.TABLE_NAMES]


class TestRunWithMockedConnection:
    def test_success_all_counts_match(self, settings, local_counts):
        cursor = FakeCursor(local_counts)
        conn = FakeConnection(cursor)

        ok = loader.run(settings, connection=conn)

        assert ok is True
        # an injected connection is never closed by run() itself
        assert conn.closed is False

        assert cursor.executed[0] == loader.schema_ddl(settings)
        assert cursor.executed[1] == loader.volume_ddl(settings)
        # then PUT, CTAS, COUNT per table, in TABLE_NAMES order
        raw_stmts = cursor.executed[2 : 2 + 3 * len(loader.TABLE_NAMES)]
        assert len(raw_stmts) == 3 * len(loader.TABLE_NAMES)
        for i, table in enumerate(loader.TABLE_NAMES):
            put, ctas, count = raw_stmts[i * 3 : i * 3 + 3]
            assert put.startswith("PUT ")
            assert table in put
            assert ctas.startswith("CREATE OR REPLACE TABLE")
            assert table in ctas
            assert count == loader.count_statement(settings, table)

        # then the medallion.sql statements (bronze/silver/gold), templated
        # with {{raw}} = settings.schema, exactly as DuckDBDriver runs them
        # locally with {{raw}} = "main" — this is the M3c "one source of
        # truth, both engines" guarantee.
        after_raw = cursor.executed[2 + 3 * len(loader.TABLE_NAMES) :]
        expected_medallion = render_statements(settings.schema)
        medallion_stmts = after_raw[: len(expected_medallion)]
        assert medallion_stmts == expected_medallion

        # then one row-count verification per gold mart, in GOLD_TABLES order
        gold_count_stmts = after_raw[len(expected_medallion) :]
        assert gold_count_stmts == [
            loader.gold_count_statement(settings, mart) for mart in GOLD_TABLES
        ]

    def test_row_count_mismatch_returns_false(self, settings, local_counts):
        bad_counts = list(local_counts)
        bad_counts[0] += 1  # first table now "mismatched"
        cursor = FakeCursor(bad_counts)
        conn = FakeConnection(cursor)

        ok = loader.run(settings, connection=conn)

        assert ok is False

    def test_row_count_mismatch_never_applies_medallion(self, settings, local_counts):
        # A raw load that fails its integrity check must not go on to build
        # bronze/silver/gold on top of possibly-bad raw tables.
        bad_counts = list(local_counts)
        bad_counts[0] += 1
        cursor = FakeCursor(bad_counts)
        conn = FakeConnection(cursor)

        loader.run(settings, connection=conn)

        assert not any(
            stmt.startswith("CREATE SCHEMA IF NOT EXISTS bronze")
            for stmt in cursor.executed
        )

    def test_connection_failure_is_caught_and_returns_false(self, settings, local_counts):
        cursor = FailingCursor(local_counts, fail_on_prefix="PUT ")
        conn = FakeConnection(cursor)

        ok = loader.run(settings, connection=conn)

        assert ok is False

    def test_medallion_failure_after_good_raw_load_returns_false(self, settings, local_counts):
        # Raw tables load and verify fine, but the medallion apply step
        # itself fails (e.g. a Databricks-only SQL rejection) — the loader
        # must report failure, not silently report success.
        cursor = FailingCursor(local_counts, fail_on_prefix="CREATE SCHEMA IF NOT EXISTS bronze")
        conn = FakeConnection(cursor)

        ok = loader.run(settings, connection=conn)

        assert ok is False
        # the raw tables' own DDL/PUT/CTAS did happen before the failure
        assert any(stmt.startswith("PUT ") for stmt in cursor.executed)

    def test_turkish_hint_for_permission_error(self):
        hint = loader.turkish_hint(RuntimeError("PERMISSION_DENIED (403): invalid access token"))
        assert "DATABRICKS_TOKEN" in hint

    def test_turkish_hint_for_warehouse_not_found(self):
        hint = loader.turkish_hint(RuntimeError("404 Not Found: no warehouse with that id"))
        assert "DATABRICKS_HTTP_PATH" in hint

    def test_turkish_hint_for_hostname_error(self):
        hint = loader.turkish_hint(RuntimeError("Temporary failure in name resolution / getaddrinfo failed"))
        assert "DATABRICKS_SERVER_HOSTNAME" in hint


# ---------------------------------------------------------------------------
# DatabricksDriver: catalog/schema passed through to databricks.sql.connect
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_databricks_module(monkeypatch):
    """Injects a fake `databricks.sql` module recording connect() kwargs,
    so DatabricksDriver's lazy `from databricks import sql as dbsql` picks
    it up with no real package and no network involved."""
    calls: dict = {}

    class FakeCursorCM:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    class FakeConnection:
        def cursor(self):
            return FakeCursorCM()

    def fake_connect(**kwargs):
        calls.update(kwargs)
        return FakeConnection()

    fake_sql = types.ModuleType("databricks.sql")
    fake_sql.connect = fake_connect
    fake_databricks = types.ModuleType("databricks")
    fake_databricks.sql = fake_sql

    monkeypatch.setitem(sys.modules, "databricks", fake_databricks)
    monkeypatch.setitem(sys.modules, "databricks.sql", fake_sql)
    return calls


class TestDatabricksDriverCatalogSchema:
    def _set_required_env(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "adb-1.azuredatabricks.net")
        monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123")
        monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-fake")

    def test_defaults_workspace_and_sonova_when_unset(self, monkeypatch, fake_databricks_module):
        self._set_required_env(monkeypatch)
        monkeypatch.delenv("DATABRICKS_CATALOG", raising=False)
        monkeypatch.delenv("DATABRICKS_SCHEMA", raising=False)

        from agent.db import DatabricksDriver

        DatabricksDriver()

        assert fake_databricks_module["catalog"] == "workspace"
        assert fake_databricks_module["schema"] == "sonova"
        assert fake_databricks_module["server_hostname"] == "adb-1.azuredatabricks.net"
        assert fake_databricks_module["http_path"] == "/sql/1.0/warehouses/abc123"
        assert fake_databricks_module["access_token"] == "dapi-fake"

    def test_passes_explicit_catalog_and_schema_from_env(self, monkeypatch, fake_databricks_module):
        self._set_required_env(monkeypatch)
        monkeypatch.setenv("DATABRICKS_CATALOG", "main")
        monkeypatch.setenv("DATABRICKS_SCHEMA", "prod_sonova")

        from agent.db import DatabricksDriver

        DatabricksDriver()

        assert fake_databricks_module["catalog"] == "main"
        assert fake_databricks_module["schema"] == "prod_sonova"

    def test_missing_required_env_raises_before_importing_connector(self, monkeypatch):
        for var in ("DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_HTTP_PATH", "DATABRICKS_TOKEN"):
            monkeypatch.delenv(var, raising=False)

        from agent.db import DatabricksDriver

        with pytest.raises(EnvironmentError):
            DatabricksDriver()


# ---------------------------------------------------------------------------
# System-prompt guidance against markdown tables/headings (Part A, step 2)
# ---------------------------------------------------------------------------
class TestSystemPromptMarkdownGuidance:
    def test_prompt_forbids_markdown_tables_and_headings(self, driver):
        from agent.agentic import AgenticFunnelAgent
        from agent.testing import ScriptedLLM

        agent = AgenticFunnelAgent(driver=driver, llm_chat=ScriptedLLM([]))
        prompt = agent._system_prompt()

        assert "markdown table" in prompt.lower()
        assert "heading" in prompt.lower()
        assert "NEVER" in prompt
