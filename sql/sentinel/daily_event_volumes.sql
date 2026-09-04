-- ============================================================================
-- daily_event_volumes.sql — sentinel check #1 (module M5, PDF task 5.5).
--
-- Purpose: for every (event_name, segment, source) series, compare the as-of
-- day's actual event count against a trailing 28-day control band — mean and
-- standard deviation computed from the 28 days STRICTLY BEFORE the as-of day
-- via window functions, never including the day being scored. Severity/band-
-- multiplier decisions are made in Python (scripts/sentinel.py via
-- agent.sentinel_core) from config/sentinel_registry.json — this file only
-- produces the actual-vs-band numbers, it never classifies severity itself.
--
-- segment: for web-sourced rows, segment = market (web_events.country); for
-- app-sourced rows, segment = platform (app_events.platform), because
-- app_events carries no market/geo field at the event grain (see
-- sql/medallion.sql's bronze.app_events comment) — platform is the closest
-- meaningful split available. The `source` column ('web' | 'app') says which
-- meaning `segment` carries in a given row.
--
-- Caveat (documented on purpose, same spirit as sql/medallion.sql's marts):
-- the trailing band walks the 28 PRECEDING rows of a partition, not 28
-- PRECEDING calendar days — a series with no rows at all on some earlier day
-- silently skips that gap rather than counting it as a zero. This is fine
-- for the high-volume series this dataset has (a gap would mean the series
-- already vanished, which the schema-drift / missing-event-today check in
-- agent.sentinel_core catches separately) but is a known limitation for a
-- genuinely sparse series — hence the registry's min_volume_floor, which
-- suppresses banding noise on series too small for a band to mean anything.
--
-- Templating: the only substitution variable is {{as_of}} (a DATE literal,
-- e.g. '2026-08-30') — history is capped at {{as_of}} so no future rows can
-- leak into the trailing band.
-- ============================================================================

WITH web_daily AS (
  -- One row per (day, event_name, market) with that day's web event count.
  SELECT
    CAST(event_timestamp AS DATE) AS day,
    event_name,
    country AS segment,
    'web' AS source,
    COUNT(*) AS event_count
  FROM web_events
  WHERE CAST(event_timestamp AS DATE) <= DATE '{{as_of}}'
  GROUP BY 1, 2, 3
),
app_daily AS (
  -- One row per (day, event_name, platform) with that day's app event count;
  -- platform stands in for segment since app_events has no market field.
  SELECT
    CAST(event_timestamp AS DATE) AS day,
    event_name,
    platform AS segment,
    'app' AS source,
    COUNT(*) AS event_count
  FROM app_events
  WHERE CAST(event_timestamp AS DATE) <= DATE '{{as_of}}'
  GROUP BY 1, 2, 3
),
all_daily AS (
  -- Union both sources into one (day, event_name, segment, source) grid.
  SELECT * FROM web_daily
  UNION ALL
  SELECT * FROM app_daily
),
banded AS (
  -- Trailing 28-day control band per series, from the 28 rows strictly
  -- BEFORE the current row — never including the day being scored, so the
  -- band reflects "what we expected", not something today can distort.
  SELECT
    day,
    event_name,
    segment,
    source,
    event_count,
    AVG(event_count) OVER w AS band_avg,
    STDDEV(event_count) OVER w AS band_stddev,
    COUNT(*) OVER w AS band_days
  FROM all_daily
  WINDOW w AS (
    PARTITION BY event_name, segment, source ORDER BY day
    ROWS BETWEEN 28 PRECEDING AND 1 PRECEDING
  )
)
SELECT
  day,
  event_name,
  segment,
  source,
  event_count AS actual_count,
  band_avg,
  band_stddev,
  band_days,
  band_avg - COALESCE(band_stddev, 0) AS band_lower,
  band_avg + COALESCE(band_stddev, 0) AS band_upper,
  -- z-like deviation of today's actual from the trailing band; NULL when the
  -- band has no spread yet to divide by (too little/too flat history).
  CASE
    WHEN band_stddev IS NULL OR band_stddev = 0 THEN NULL
    ELSE (event_count - band_avg) / band_stddev
  END AS deviation_z
FROM banded
WHERE day = DATE '{{as_of}}'
ORDER BY source, event_name, segment;
