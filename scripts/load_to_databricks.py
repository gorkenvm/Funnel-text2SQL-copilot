#!/usr/bin/env python3
"""One-command loader: pushes the local synthetic parquet files into a
Databricks Unity Catalog volume, materializes them as raw tables, and then
builds the bronze/silver/gold medallion layer on top of them (modules M3b +
M3c).

Run this once, on your own machine, after adding the Databricks connection
values to your repo-root ``.env`` (see ``docs/deploy_guide.md``, section 5):

    python scripts/load_to_databricks.py

What it does, in order (idempotent — safe to re-run):

1. ``CREATE SCHEMA IF NOT EXISTS <catalog>.<schema>``
2. ``CREATE VOLUME IF NOT EXISTS <catalog>.<schema>.raw``
3. For each of ``web_events``, ``app_events``, ``id_bridge`` (deliberately
   NOT ``_ground_truth``, which never leaves ``data/``):
   a. ``PUT`` the local parquet file into the volume (staging ingestion,
      ``OVERWRITE``);
   b. ``CREATE OR REPLACE TABLE <name> AS SELECT * FROM parquet.`...` ``;
   c. ``SELECT COUNT(*)`` and compare against the local row count (read via
      pyarrow) as a load-integrity check.
4. Once every raw table is verified: apply ``sql/medallion.sql`` (via
   ``agent.medallion.apply_medallion``) against this same connection, with
   ``{{raw}} = <schema>`` — building ``bronze``/``silver``/``gold`` schemas
   in the same catalog. This is the EXACT same SQL DuckDBDriver runs
   in-memory for local development; see ``sql/medallion.sql``.
5. Print a row-count verification for every gold mart.

Requires network access to your Databricks workspace — this script is not
exercised by the offline pytest suite (see ``tests/test_databricks_loader.py``
for the parts of it that ARE unit-tested without a network: SQL-statement
generation and environment validation).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent.db import (  # noqa: E402
    DEFAULT_DATABRICKS_CATALOG,
    DEFAULT_DATABRICKS_SCHEMA,
)
from agent.medallion import GOLD_TABLES, apply_medallion  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
ENV_PATH = REPO_ROOT / ".env"

#: Local parquet files to load. Deliberately excludes _ground_truth.parquet,
#: which must never leave the generator's local data/ directory (see the
#: project rule: never touch data/ or generate_data.py).
TABLE_NAMES: tuple[str, ...] = ("web_events", "app_events", "id_bridge")

VOLUME_NAME = "raw"

REQUIRED_ENV_VARS: tuple[str, ...] = (
    "DATABRICKS_SERVER_HOSTNAME",
    "DATABRICKS_HTTP_PATH",
    "DATABRICKS_TOKEN",
)


class ConfigError(Exception):
    """Raised when a required environment variable is missing."""


@dataclass(frozen=True)
class Settings:
    """Resolved connection settings for one loader run."""

    server_hostname: str
    http_path: str
    token: str
    catalog: str
    schema: str


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Validate and collect connection settings from ``env`` (default: ``os.environ``).

    Raises :class:`ConfigError` naming every missing required variable.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV_VARS if not source.get(name)]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )
    return Settings(
        server_hostname=source["DATABRICKS_SERVER_HOSTNAME"],
        http_path=source["DATABRICKS_HTTP_PATH"],
        token=source["DATABRICKS_TOKEN"],
        catalog=source.get("DATABRICKS_CATALOG") or DEFAULT_DATABRICKS_CATALOG,
        schema=source.get("DATABRICKS_SCHEMA") or DEFAULT_DATABRICKS_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Pure SQL-statement builders — unit-testable without any live connection.
# ---------------------------------------------------------------------------
def schema_ddl(settings: Settings) -> str:
    return f"CREATE SCHEMA IF NOT EXISTS {settings.catalog}.{settings.schema}"


def volume_ddl(settings: Settings) -> str:
    return (
        "CREATE VOLUME IF NOT EXISTS "
        f"{settings.catalog}.{settings.schema}.{VOLUME_NAME}"
    )


def volume_path(settings: Settings, table: str) -> str:
    return f"/Volumes/{settings.catalog}/{settings.schema}/{VOLUME_NAME}/{table}.parquet"


def put_statement(local_path: Path, settings: Settings, table: str) -> str:
    return (
        f"PUT '{local_path.as_posix()}' INTO '{volume_path(settings, table)}' OVERWRITE"
    )


def ctas_statement(settings: Settings, table: str) -> str:
    fq_table = f"{settings.catalog}.{settings.schema}.{table}"
    return (
        f"CREATE OR REPLACE TABLE {fq_table} AS "
        f"SELECT * FROM parquet.`{volume_path(settings, table)}`"
    )


def count_statement(settings: Settings, table: str) -> str:
    return f"SELECT COUNT(*) AS n FROM {settings.catalog}.{settings.schema}.{table}"


def gold_count_statement(settings: Settings, mart: str) -> str:
    """Row-count statement for one gold mart, fully qualified by catalog."""
    return f"SELECT COUNT(*) AS n FROM {settings.catalog}.gold.{mart}"


def local_parquet_path(table: str, data_dir: Path = DATA_DIR) -> Path:
    path = data_dir / f"{table}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Expected local parquet file not found: {path}")
    return path


def ensure_us_timestamps(src: Path, data_dir: Path = DATA_DIR) -> Path:
    """Return a parquet path whose timestamp columns are microsecond precision.

    pandas writes ``timestamp[ns]`` parquet, which Databricks' reader rejects
    (``PARQUET_TYPE_ILLEGAL: INT64 (TIMESTAMP(NANOS,false))``). If ``src``
    contains any nanosecond timestamp column, write a converted copy under
    ``<data_dir>/_staging/`` (still inside ``staging_allowed_local_path``)
    and return it; otherwise return ``src`` unchanged.
    """
    import pyarrow as pa

    table = pq.read_table(src)
    has_ns = any(
        pa.types.is_timestamp(f.type) and f.type.unit == "ns" for f in table.schema
    )
    if not has_ns:
        return src
    staging_dir = data_dir / "_staging"
    staging_dir.mkdir(exist_ok=True)
    out = staging_dir / src.name
    # coerce_timestamps truncates sub-microsecond precision, which is exactly
    # what we want here (synthetic event times; microseconds are plenty).
    pq.write_table(
        table, out, coerce_timestamps="us", allow_truncated_timestamps=True
    )
    return out


def local_row_count(table: str, data_dir: Path = DATA_DIR) -> int:
    """Local ground-truth row count for ``table``, read directly via pyarrow."""
    return pq.read_table(local_parquet_path(table, data_dir)).num_rows


# ---------------------------------------------------------------------------
# Turkish hints for the failure modes a user is most likely to hit.
# ---------------------------------------------------------------------------
def turkish_hint(exc: Exception) -> str:
    text = str(exc).lower()
    if any(s in text for s in ("403", "forbidden", "invalid access token", "unauthorized", "authentication")):
        return "İpucu: DATABRICKS_TOKEN yanlış veya süresi dolmuş olabilir (403/401 hatası)."
    if any(s in text for s in ("404", "not found", "does not exist", "no warehouse", "cluster")):
        return "İpucu: DATABRICKS_HTTP_PATH yanlış olabilir — warehouse bulunamadı."
    if any(s in text for s in ("name resolution", "getaddrinfo", "could not resolve", "nodename nor servname")):
        return "İpucu: DATABRICKS_SERVER_HOSTNAME yanlış olabilir — sunucu adı çözümlenemedi."
    if "timed out" in text or "timeout" in text:
        return "İpucu: Bağlantı zaman aşımına uğradı — VPN/ağ ve warehouse'un çalışır (uyanık) olduğunu kontrol edin."
    if "permission" in text or "access denied" in text or "unity catalog" in text:
        return "İpucu: Token'ın bu katalog/şema üzerinde yetkisi olmayabilir."
    if "staging_allowed_local_path" in text:
        return "İpucu: Yerel dosya yolu izinli staging dizininin dışında — data/ klasörünü kontrol edin."
    return "İpucu: Yukarıdaki ham hatayı Databricks SQL Warehouse loglarıyla karşılaştırın."


def _step(message: str) -> None:
    print(f"-> {message}")


def _fail(step: str, exc: Exception) -> None:
    print(f"ERROR [{step}]: {exc}")
    print(turkish_hint(exc))


def run(settings: Settings, connection: Optional[Any] = None) -> bool:
    """Run the full load + medallion build against ``connection`` (live or mocked).

    Returns ``True`` iff every raw table loaded with a matching row count
    AND the bronze/silver/gold medallion layer (``sql/medallion.sql``)
    applied without error. ``connection`` is injectable so this function is
    unit-testable without any network access; when omitted, a real
    ``databricks.sql`` connection is opened and closed here.
    """
    owns_connection = connection is None
    if connection is None:
        try:
            from databricks import sql as dbsql  # lazy import by design
        except ImportError as exc:
            _fail("import", exc)
            return False
        try:
            connection = dbsql.connect(
                server_hostname=settings.server_hostname,
                http_path=settings.http_path,
                access_token=settings.token,
                catalog=settings.catalog,
                schema=settings.schema,
                staging_allowed_local_path=str(DATA_DIR),
            )
        except Exception as exc:  # noqa: BLE001 - surface any connect-time failure
            _fail("connect", exc)
            return False

    summary: list[tuple[str, int, int]] = []
    try:
        cursor = connection.cursor()
        try:
            _step(f"Creating schema {settings.catalog}.{settings.schema} (if missing)...")
            cursor.execute(schema_ddl(settings))

            _step(f"Creating volume {settings.catalog}.{settings.schema}.{VOLUME_NAME} (if missing)...")
            cursor.execute(volume_ddl(settings))

            for table in TABLE_NAMES:
                _step(f"[{table}] reading local row count (pyarrow)...")
                local_count = local_row_count(table)

                _step(f"[{table}] ensuring Databricks-compatible timestamps (ns -> us)...")
                upload_path = ensure_us_timestamps(local_parquet_path(table))

                _step(f"[{table}] uploading parquet to the volume (PUT ... OVERWRITE)...")
                cursor.execute(put_statement(upload_path, settings, table))

                _step(f"[{table}] materializing table (CREATE OR REPLACE TABLE)...")
                cursor.execute(ctas_statement(settings, table))

                _step(f"[{table}] verifying row count...")
                cursor.execute(count_statement(settings, table))
                row = cursor.fetchone()
                remote_count = int(row[0]) if row is not None else -1
                summary.append((table, local_count, remote_count))

            print("\nRaw layer verification:")
            raw_ok = True
            for table, local_count, remote_count in summary:
                ok = local_count == remote_count
                raw_ok = raw_ok and ok
                print(
                    f"  {table}: local={local_count} databricks={remote_count} "
                    f"[{'OK' if ok else 'MISMATCH'}]"
                )

            if not raw_ok:
                print(
                    "\nRaw load row-count mismatch — skipping medallion "
                    "layers (bronze/silver/gold) until this is fixed."
                )
                return False

            _step(
                "Applying medallion layers (bronze/silver/gold) from "
                "sql/medallion.sql ..."
            )
            apply_medallion(cursor.execute, raw_schema=settings.schema)

            _step("Verifying gold layer row counts...")
            print("\nGold layer verification:")
            for mart in GOLD_TABLES:
                cursor.execute(gold_count_statement(settings, mart))
                row = cursor.fetchone()
                n = int(row[0]) if row is not None else -1
                print(f"  gold.{mart}: {n} row(s)")
        finally:
            cursor.close()
    except Exception as exc:  # noqa: BLE001 - never let a raw traceback be the only output
        _fail("load", exc)
        return False
    finally:
        if owns_connection:
            connection.close()

    return True


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH, override=False)
    except ImportError:  # pragma: no cover - python-dotenv is a real dependency
        pass

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        print(
            "İpucu: .env dosyanıza DATABRICKS_SERVER_HOSTNAME, "
            "DATABRICKS_HTTP_PATH ve DATABRICKS_TOKEN satırlarını ekleyin "
            "(bkz. docs/deploy_guide.md, Bölüm 5)."
        )
        return 1

    print(
        f"Target: {settings.catalog}.{settings.schema} "
        f"(server: {settings.server_hostname})"
    )
    ok = run(settings)
    if ok:
        print(
            "\nDone: raw tables loaded and verified, and the bronze/silver/"
            "gold medallion layer was built successfully."
        )
        return 0
    print("\nLoad failed — see the error above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
