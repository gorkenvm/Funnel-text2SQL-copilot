"""Database driver abstraction.

Two interchangeable drivers expose the same tiny interface:

* :class:`DuckDBDriver` — in-memory DuckDB over local parquet files
  (development, tests, demos).
* :class:`DatabricksDriver` — same interface over a Databricks SQL
  warehouse (production); imported lazily so the package works without
  ``databricks-sql-connector`` installed.

Only the three analytics tables are registered/whitelisted:
``web_events``, ``app_events``, ``id_bridge``.  The generator's ground
truth is deliberately NOT exposed to the agent.

Since module M3c both drivers additionally build the bronze/silver/gold
medallion layers on top of those three raw tables, from the single
versioned ``sql/medallion.sql`` file (see :mod:`agent.medallion`):
``DuckDBDriver`` applies it at construction time, in-memory;
``scripts/load_to_databricks.py`` applies it once, after loading the raw
parquet files into Databricks.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import pandas as pd

from agent.medallion import apply_medallion

#: Tables the agent is allowed to see, mapped to their parquet file names.
EXPOSED_TABLES: dict[str, str] = {
    "web_events": "web_events.parquet",
    "app_events": "app_events.parquet",
    "id_bridge": "id_bridge.parquet",
}


def _default_data_dir() -> Path:
    """Resolve the local data directory (env override, else <repo>/data)."""
    env = os.environ.get("AGENT_DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data"


class BaseDriver(ABC):
    """Minimal driver contract: run SQL, get a pandas DataFrame back."""

    @abstractmethod
    def query(self, sql: str) -> pd.DataFrame:
        """Execute a (already guard-railed) SQL statement and return rows."""
        raise NotImplementedError


#: Schema the raw parquet-backed views land in inside DuckDB's default
#: ("memory") catalog — this is the {{raw}} value substituted into
#: sql/medallion.sql when DuckDBDriver applies it.
DUCKDB_RAW_SCHEMA = "main"


class DuckDBDriver(BaseDriver):
    """In-memory DuckDB with the three parquet files registered as views,
    plus the full bronze/silver/gold medallion layer built on top of them
    (see :func:`agent.medallion.apply_medallion`)."""

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        import duckdb  # local import keeps module import cheap

        self._data_dir = Path(data_dir) if data_dir else _default_data_dir()
        self._con = duckdb.connect(database=":memory:")
        for view_name, file_name in EXPOSED_TABLES.items():
            path = self._data_dir / file_name
            if not path.exists():
                raise FileNotFoundError(f"Expected parquet file not found: {path}")
            # Parameterized path is not supported in DDL; the path comes from
            # trusted config (EXPOSED_TABLES + data_dir), not user input.
            self._con.execute(
                f"CREATE OR REPLACE VIEW {view_name} AS "
                f"SELECT * FROM read_parquet('{path.as_posix()}')"
            )
        apply_medallion(self._con.execute, raw_schema=DUCKDB_RAW_SCHEMA)

    def query(self, sql: str) -> pd.DataFrame:
        return self._con.execute(sql).fetchdf()


#: Defaults for the two-part namespace the agent's bare table names
#: (web_events, app_events, id_bridge) are resolved under when connecting
#: to Databricks. Overridable via DATABRICKS_CATALOG / DATABRICKS_SCHEMA so
#: they always match what scripts/load_to_databricks.py created.
DEFAULT_DATABRICKS_CATALOG = "workspace"
DEFAULT_DATABRICKS_SCHEMA = "funnel"


class DatabricksDriver(BaseDriver):
    """Driver for a Databricks SQL warehouse.

    Reads connection settings from the environment:

    * ``DATABRICKS_SERVER_HOSTNAME``
    * ``DATABRICKS_HTTP_PATH``
    * ``DATABRICKS_TOKEN``
    * ``DATABRICKS_CATALOG`` (optional, default ``"workspace"``)
    * ``DATABRICKS_SCHEMA`` (optional, default ``"funnel"``)

    The ``databricks`` package is imported lazily inside ``__init__`` so
    that installing it is only required when this driver is actually used.
    ``catalog``/``schema`` are passed straight to ``databricks.sql.connect``
    so the connection's default namespace matches wherever
    ``scripts/load_to_databricks.py`` created the three tables, and the
    agent's bare table names (``web_events``, ``app_events``, ``id_bridge``)
    resolve without qualification — mirroring the local DuckDB driver's
    unqualified view names.
    """

    def __init__(self) -> None:
        try:
            from databricks import sql as dbsql  # lazy import by design
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "DatabricksDriver requires the optional dependency "
                "'databricks-sql-connector' (pip install databricks-sql-connector)."
            ) from exc

        missing = [
            var
            for var in (
                "DATABRICKS_SERVER_HOSTNAME",
                "DATABRICKS_HTTP_PATH",
                "DATABRICKS_TOKEN",
            )
            if not os.environ.get(var)
        ]
        if missing:
            raise EnvironmentError(
                "Missing required environment variables for Databricks: "
                + ", ".join(missing)
            )

        catalog = os.environ.get("DATABRICKS_CATALOG") or DEFAULT_DATABRICKS_CATALOG
        schema = os.environ.get("DATABRICKS_SCHEMA") or DEFAULT_DATABRICKS_SCHEMA

        self._connection = dbsql.connect(
            server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
            http_path=os.environ["DATABRICKS_HTTP_PATH"],
            access_token=os.environ["DATABRICKS_TOKEN"],
            catalog=catalog,
            schema=schema,
        )

    def query(self, sql: str) -> pd.DataFrame:
        with self._connection.cursor() as cursor:  # pragma: no cover - needs warehouse
            cursor.execute(sql)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
        return pd.DataFrame(rows, columns=columns)


def get_driver(name: Optional[str] = None) -> BaseDriver:
    """Factory: build a driver by name.

    Resolution order: explicit ``name`` argument, then the ``AGENT_DB``
    environment variable, then the default ``"duckdb"``.
    """
    resolved = (name or os.environ.get("AGENT_DB") or "duckdb").strip().lower()
    if resolved == "duckdb":
        return DuckDBDriver()
    if resolved == "databricks":
        return DatabricksDriver()
    raise ValueError(f"Unknown driver '{resolved}'. Use 'duckdb' or 'databricks'.")
