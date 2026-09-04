-- ============================================================================
-- daily_funnel_rates.sql — sentinel check #2 (module M5, PDF task 5.5).
--
-- Purpose: for each of the three funnel steps below, compare the as-of day's
-- step-conversion rate against a trailing 28-day control band (mean and
-- standard deviation from the 28 days STRICTLY BEFORE the as-of day),
-- exactly the same pattern as daily_event_volumes.sql. Severity is decided
-- in Python (agent.sentinel_core) from config/sentinel_registry.json.
--
--   * start_to_complete    — hearing_test_start -> hearing_test_complete,
--                            web grain.
--   * complete_to_download — hearing_test_complete -> app_open, restricted
--                            to the bridge population (id_bridge.opt_in_flag
--                            = true). This is the one cross-device step, so
--                            it goes through the same consent gate as
--                            silver.v_attribution_eligible in
--                            sql/medallion.sql.
--   * download_to_pair     — app_open -> hearing_aid_paired, app grain only
--                            (both sides already share the app id space, no
--                            bridge needed).
--
-- Caveat (documented on purpose, same spirit as sql/medallion.sql's marts):
-- these are same-day ACTIVITY ratios -- a day's "to" event count over that
-- same day's "from" event count -- not a cohort/first-occurrence conversion
-- rate like gold.step_conversion. They exist purely as a fast, cheap
-- day-over-day drift signal; they are not a KPI of record.
--
-- Templating: the only substitution variable is {{as_of}} (a DATE literal,
-- e.g. '2026-08-30') — history is capped at {{as_of}} so no future rows can
-- leak into the trailing band.
-- ============================================================================

WITH bridge_population AS (
  -- Consented, signed-in bridge rows only -- the one gate any cross-device
  -- (web -> app) count in this file is allowed to go through, mirroring
  -- silver.v_attribution_eligible in sql/medallion.sql.
  SELECT web_pseudo_id, app_device_id
  FROM id_bridge
  WHERE opt_in_flag = true
),
start_daily AS (
  -- Daily hearing_test_start count, web grain.
  SELECT CAST(event_timestamp AS DATE) AS day, COUNT(*) AS n
  FROM web_events
  WHERE event_name = 'hearing_test_start'
    AND CAST(event_timestamp AS DATE) <= DATE '{{as_of}}'
  GROUP BY 1
),
complete_daily AS (
  -- Daily hearing_test_complete count, web grain, all users.
  SELECT CAST(event_timestamp AS DATE) AS day, COUNT(*) AS n
  FROM web_events
  WHERE event_name = 'hearing_test_complete'
    AND CAST(event_timestamp AS DATE) <= DATE '{{as_of}}'
  GROUP BY 1
),
bridged_complete_daily AS (
  -- Daily hearing_test_complete count, bridge population only.
  SELECT CAST(w.event_timestamp AS DATE) AS day, COUNT(*) AS n
  FROM web_events w
  JOIN bridge_population b ON b.web_pseudo_id = w.user_pseudo_id
  WHERE w.event_name = 'hearing_test_complete'
    AND CAST(w.event_timestamp AS DATE) <= DATE '{{as_of}}'
  GROUP BY 1
),
bridged_download_daily AS (
  -- Daily app_open count, bridge population only.
  SELECT CAST(a.event_timestamp AS DATE) AS day, COUNT(*) AS n
  FROM app_events a
  JOIN bridge_population b ON b.app_device_id = a.hashed_device_id
  WHERE a.event_name = 'app_open'
    AND CAST(a.event_timestamp AS DATE) <= DATE '{{as_of}}'
  GROUP BY 1
),
download_daily AS (
  -- Daily app_open count, app grain, all devices.
  SELECT CAST(event_timestamp AS DATE) AS day, COUNT(*) AS n
  FROM app_events
  WHERE event_name = 'app_open'
    AND CAST(event_timestamp AS DATE) <= DATE '{{as_of}}'
  GROUP BY 1
),
pair_daily AS (
  -- Daily hearing_aid_paired count, app grain, all devices.
  SELECT CAST(event_timestamp AS DATE) AS day, COUNT(*) AS n
  FROM app_events
  WHERE event_name = 'hearing_aid_paired'
    AND CAST(event_timestamp AS DATE) <= DATE '{{as_of}}'
  GROUP BY 1
),
step_start_complete AS (
  -- Step 1 grid: start -> complete, web grain.
  SELECT
    COALESCE(s.day, c.day) AS day,
    'start_to_complete' AS step,
    COALESCE(s.n, 0) AS from_count,
    COALESCE(c.n, 0) AS to_count
  FROM start_daily s
  FULL OUTER JOIN complete_daily c ON c.day = s.day
),
step_complete_download AS (
  -- Step 2 grid: complete -> download, bridge population only.
  SELECT
    COALESCE(c.day, d.day) AS day,
    'complete_to_download' AS step,
    COALESCE(c.n, 0) AS from_count,
    COALESCE(d.n, 0) AS to_count
  FROM bridged_complete_daily c
  FULL OUTER JOIN bridged_download_daily d ON d.day = c.day
),
step_download_pair AS (
  -- Step 3 grid: download -> pair, app grain, all devices.
  SELECT
    COALESCE(d.day, p.day) AS day,
    'download_to_pair' AS step,
    COALESCE(d.n, 0) AS from_count,
    COALESCE(p.n, 0) AS to_count
  FROM download_daily d
  FULL OUTER JOIN pair_daily p ON p.day = d.day
),
step_daily AS (
  -- All three steps stacked into one (day, step, from_count, to_count) grid.
  SELECT * FROM step_start_complete
  UNION ALL
  SELECT * FROM step_complete_download
  UNION ALL
  SELECT * FROM step_download_pair
),
rated AS (
  -- Same-day conversion rate per step (NULL, not zero, when from_count = 0
  -- -- an undefined rate must never masquerade as a 0% rate).
  SELECT
    day,
    step,
    from_count,
    to_count,
    1.0 * to_count / NULLIF(from_count, 0) AS conversion_rate
  FROM step_daily
),
banded AS (
  -- Trailing 28-day control band per step, from the 28 rows strictly BEFORE
  -- the current row's day -- never including the day being scored.
  SELECT
    day,
    step,
    from_count,
    to_count,
    conversion_rate,
    AVG(conversion_rate) OVER w AS band_avg,
    STDDEV(conversion_rate) OVER w AS band_stddev,
    COUNT(conversion_rate) OVER w AS band_days
  FROM rated
  WINDOW w AS (
    PARTITION BY step ORDER BY day
    ROWS BETWEEN 28 PRECEDING AND 1 PRECEDING
  )
)
SELECT
  day,
  step,
  from_count,
  to_count,
  conversion_rate AS actual_rate,
  band_avg,
  band_stddev,
  band_days,
  band_avg - COALESCE(band_stddev, 0) AS band_lower,
  band_avg + COALESCE(band_stddev, 0) AS band_upper,
  -- z-like deviation of today's rate from the trailing band; NULL when the
  -- band has no spread yet to divide by (too little/too flat history).
  CASE
    WHEN band_stddev IS NULL OR band_stddev = 0 THEN NULL
    ELSE (conversion_rate - band_avg) / band_stddev
  END AS deviation_z
FROM banded
WHERE day = DATE '{{as_of}}'
ORDER BY step;
