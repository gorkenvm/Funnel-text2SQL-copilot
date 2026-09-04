-- ============================================================================
-- schema_snapshot.sql — sentinel check #3 (module M5, PDF task 5.5).
--
-- Purpose: a LIVE inventory of the three raw event tables (web_events,
-- app_events, id_bridge) — column/type list, plus the distinct event_name
-- and app_version values currently in production. agent.sentinel_core diffs
-- this against config/sentinel_registry.json's expected_* baseline to raise
-- schema-drift findings (new/missing column, type change, new/missing event
-- name, new app_version). No {{as_of}} here: a schema snapshot is a "right
-- now" read, not a dated one.
--
-- Multi-statement file, in the same '...;' one-statement-per-block, '--'
-- full-line-comment style as sql/medallion.sql, PLUS one convention of its
-- own: a `-- @check: <name>` marker line immediately above each statement
-- names it, so agent.sentinel_core.load_named_statements() can hand the
-- script/notebook each block by name instead of by fragile position.
--
-- Cost note: every statement below is a GROUP BY / catalog-metadata read,
-- never a raw row dump — "cheap aggregates, not full scans where avoidable".
-- ============================================================================

-- @check: columns
-- Live table/column/type inventory for the three raw event tables, scoped to
-- the connection's own default catalog/schema so this reads the RAW layer
-- (not a same-named bronze/silver/gold copy) on either engine — current_
-- catalog()/current_schema() resolve to "memory"/"main" on DuckDB and to
-- DATABRICKS_CATALOG/DATABRICKS_SCHEMA (e.g. "workspace"/"sonova") on
-- Databricks, exactly where scripts/load_to_databricks.py put the raw
-- tables before the medallion layers were built on top of them.
SELECT
  table_name,
  column_name,
  data_type,
  ordinal_position
FROM information_schema.columns
WHERE table_schema = current_schema()
  AND table_catalog = current_catalog()
  AND table_name IN ('web_events', 'app_events', 'id_bridge')
ORDER BY table_name, ordinal_position;

-- @check: event_names
-- Distinct event_name values currently observed in each event table (a
-- GROUP BY, not a raw event dump) — the schema-drift signal for "a new
-- event type appeared" or "a known one stopped being emitted entirely".
SELECT 'web_events' AS table_name, event_name
FROM web_events
GROUP BY event_name
UNION ALL
SELECT 'app_events' AS table_name, event_name
FROM app_events
GROUP BY event_name
ORDER BY table_name, event_name;

-- @check: app_versions
-- Distinct app_version values currently observed in app_events — a new,
-- unregistered release train showing up in production data is itself worth
-- an analyst's eyes (could be a rollout ahead of schedule, could be spoofed
-- traffic, could be a client bug).
SELECT app_version
FROM app_events
GROUP BY app_version
ORDER BY app_version;
