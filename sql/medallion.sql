-- ============================================================================
-- medallion.sql — single source of truth for the bronze/silver/gold lakehouse
-- layers of the ask-the-funnel project (module M3c).
--
-- This file is applied IDENTICALLY by both engines via
-- agent.medallion.apply_medallion():
--   * DuckDB   — at DuckDBDriver startup, in-memory, right after the three
--                raw parquet files are registered as views under "main".
--   * Databricks — by scripts/load_to_databricks.py, right after the raw
--                parquet files are loaded into a staging schema.
--
-- Templating: the ONLY substitution variable is {{raw}} — the schema that
-- holds the three raw tables (web_events, app_events, id_bridge) BEFORE this
-- file runs. It resolves to:
--   * "main"                          on DuckDB (where the parquet views live)
--   * <DATABRICKS_SCHEMA> (e.g. "sonova") on Databricks, under the connection's
--                                       default catalog (e.g. "workspace")
-- Every other identifier here is bare (schema.table, no catalog) so it
-- resolves under whatever catalog the calling connection defaults to —
-- DuckDB's implicit "memory" catalog, or the catalog
-- scripts/load_to_databricks.py connected with.
--
-- Statement format: one statement per '...;', comment lines start with --.
-- The applier splits on ';', strips comment lines, and executes non-empty
-- statements in file order. COMMENT ON statements are best-effort (logged,
-- not fatal, in case a target engine/version rejects one) — everything else
-- is fatal on failure.
--
-- Dialect note: date arithmetic uses `timestamp + INTERVAL n DAY` /
-- `DATE_TRUNC('week', ts)`, which is accepted by both DuckDB and Databricks
-- SQL (Spark SQL) — this was already relied upon, unmodified, by the
-- pre-M3c metrics.yaml SQL. It has been exercised here only against DuckDB;
-- see docs/deploy_guide.md's medallion section for the one Databricks-only
-- verification step this sandbox cannot perform.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- SCHEMAS
-- ----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;

-- ============================================================================
-- BRONZE — raw events, copied verbatim from the load schema. No filtering,
-- no renaming, no type coercion: bronze is the untouched landing layer.
-- ============================================================================

CREATE OR REPLACE TABLE bronze.web_events AS
SELECT * FROM {{raw}}.web_events;

COMMENT ON TABLE bronze.web_events IS
  'Raw web analytics events (page_view, hearing_test_start/complete, result_screen_view, app_store_redirect), one row per event. Grain: event. Caveat: consent_state is self-reported per event and is NOT a join gate here — only silver.v_attribution_eligible governs cross-device joins.';

CREATE OR REPLACE TABLE bronze.app_events AS
SELECT * FROM {{raw}}.app_events;

COMMENT ON TABLE bronze.app_events IS
  'Raw mobile app events (app_open, hearing_aid_paired, remote_support_session) keyed by hashed_device_id, one row per event. Grain: event. Caveat: no market/geo field exists at this grain — market is only knowable via the identity bridge.';

CREATE OR REPLACE TABLE bronze.id_bridge AS
SELECT * FROM {{raw}}.id_bridge;

COMMENT ON TABLE bronze.id_bridge IS
  'Raw identity-bridge rows linking a web_pseudo_id to an app_device_id. Grain: one row per linked identity. Caveat: contains ONLY consented, signed-in users — never treat as the full user population; use silver.v_attribution_eligible, not this table, for any cross-device join.';

-- ============================================================================
-- SILVER — cleaned, user/device-grain conformed dimensions and the ONE
-- governed gate for cross-device joins.
-- ============================================================================

-- silver.web_user_stages: one row per web pseudonymous user, with the funnel
-- milestones a user reaches on web and the attribution fields needed by the
-- channel/device gold marts.
CREATE OR REPLACE TABLE silver.web_user_stages AS
WITH ordered_sessions AS (
  SELECT
    user_pseudo_id,
    utm_campaign,
    event_timestamp,
    ROW_NUMBER() OVER (
      PARTITION BY user_pseudo_id ORDER BY event_timestamp DESC
    ) AS rn
  FROM bronze.web_events
),
last_touch AS (
  -- utm_campaign of the user's most recent session (window function, not
  -- MAX/aggregate) — this is the "last touch" attribution field.
  SELECT
    user_pseudo_id,
    COALESCE(utm_campaign, 'organic/none') AS last_touch_campaign
  FROM ordered_sessions
  WHERE rn = 1
),
attrs AS (
  SELECT
    user_pseudo_id,
    MIN(device_category) AS device_category,
    MIN(country) AS market,
    MIN(consent_state) AS consent_state
  FROM bronze.web_events
  GROUP BY user_pseudo_id
),
starts AS (
  SELECT user_pseudo_id, MIN(event_timestamp) AS first_test_start_ts
  FROM bronze.web_events
  WHERE event_name = 'hearing_test_start'
  GROUP BY user_pseudo_id
),
completes AS (
  SELECT user_pseudo_id, MIN(event_timestamp) AS first_test_complete_ts
  FROM bronze.web_events
  WHERE event_name = 'hearing_test_complete'
  GROUP BY user_pseudo_id
),
redirects AS (
  SELECT user_pseudo_id, MIN(event_timestamp) AS app_store_redirect_ts
  FROM bronze.web_events
  WHERE event_name = 'app_store_redirect'
  GROUP BY user_pseudo_id
)
SELECT
  a.user_pseudo_id,
  a.market,
  a.device_category,
  lt.last_touch_campaign,
  s.first_test_start_ts,
  c.first_test_complete_ts,
  r.app_store_redirect_ts,
  a.consent_state
FROM attrs a
LEFT JOIN last_touch lt ON lt.user_pseudo_id = a.user_pseudo_id
LEFT JOIN starts s ON s.user_pseudo_id = a.user_pseudo_id
LEFT JOIN completes c ON c.user_pseudo_id = a.user_pseudo_id
LEFT JOIN redirects r ON r.user_pseudo_id = a.user_pseudo_id;

COMMENT ON TABLE silver.web_user_stages IS
  'One row per web pseudonymous user (user_pseudo_id). Carries market/device_category/consent_state, last_touch_campaign (utm of the most recent session), and first-occurrence timestamps for hearing_test_start/complete and app_store_redirect (NULL = milestone never reached). Caveat: ~6% of users have a completion event with no recorded start (data-quality noise from the generator) — gold marts filter on first_test_start_ts IS NOT NULL where the original KPIs did, so this noise is excluded identically to before.';

-- silver.app_user_stages: one row per app device, with the funnel milestones
-- reached on app and the D30/censoring flags used by retention KPIs.
CREATE OR REPLACE TABLE silver.app_user_stages AS
WITH opens AS (
  SELECT hashed_device_id, MIN(event_timestamp) AS first_open_ts
  FROM bronze.app_events
  WHERE event_name = 'app_open'
  GROUP BY hashed_device_id
),
platforms AS (
  SELECT hashed_device_id, MIN(platform) AS platform
  FROM bronze.app_events
  GROUP BY hashed_device_id
),
paired AS (
  SELECT hashed_device_id, MIN(event_timestamp) AS paired_ts
  FROM bronze.app_events
  WHERE event_name = 'hearing_aid_paired'
  GROUP BY hashed_device_id
),
horizon AS (
  SELECT MAX(event_timestamp) AS max_ts FROM bronze.app_events
),
d30_hits AS (
  -- Any app_open 28-34 days (inclusive) after the device's first open.
  SELECT DISTINCT o.hashed_device_id
  FROM opens o
  JOIN bronze.app_events e
    ON e.hashed_device_id = o.hashed_device_id
   AND e.event_name = 'app_open'
   AND e.event_timestamp >= o.first_open_ts + INTERVAL 28 DAY
   AND e.event_timestamp <  o.first_open_ts + INTERVAL 35 DAY
)
SELECT
  o.hashed_device_id,
  p.platform,
  o.first_open_ts,
  pr.paired_ts,
  (d.hashed_device_id IS NOT NULL) AS d30_active,
  (o.first_open_ts > h.max_ts - INTERVAL 34 DAY) AS censored
FROM opens o
CROSS JOIN horizon h
LEFT JOIN platforms p ON p.hashed_device_id = o.hashed_device_id
LEFT JOIN paired pr ON pr.hashed_device_id = o.hashed_device_id
LEFT JOIN d30_hits d ON d.hashed_device_id = o.hashed_device_id;

COMMENT ON TABLE silver.app_user_stages IS
  'One row per app device (hashed_device_id) that has at least one app_open event. platform is stable per device; paired_ts is the first hearing_aid_paired event (NULL = never paired); d30_active = any app_open 28-34 days (inclusive) after first_open_ts; censored = first_open_ts is within 34 days of the dataset''s max event timestamp (right-censored — D30 status not yet observable). Caveat: censored devices always compute d30_active = false for lack of future data; retention KPIs must still filter WHERE NOT censored to report an honest denominator.';

-- silver.v_attribution_eligible — THE governance gate: row-level cross-device
-- joins must go through this view, never straight off bronze.id_bridge.
CREATE OR REPLACE VIEW silver.v_attribution_eligible AS
SELECT *
FROM bronze.id_bridge
WHERE opt_in_flag = true;

COMMENT ON VIEW silver.v_attribution_eligible IS
  'Row-level cross-device joins must go through this view: bronze.id_bridge filtered to opt_in_flag = true. Grain: one row per consented, signed-in linked identity (hashed_id). Caveat: in this dataset opt_in_flag is true for every bridge row, so this view is a pass-through today — it still exists as the single place a future partial-consent dataset would need one code change, not a scan of every downstream query.';

-- silver.linked_journeys — the temporally-ordered cross-device journey:
-- web completion -> (later) first app open, for consented+signed-in,
-- bridge-linked users only. Carries BOTH first-touch (acquisition_channel,
-- from the bridge) and last-touch (last_touch_campaign, from web) so gold
-- marts can pick whichever attribution model a KPI calls for.
CREATE OR REPLACE TABLE silver.linked_journeys AS
SELECT
  e.hashed_id,
  e.market,
  e.acquisition_channel,
  w.last_touch_campaign,
  w.first_test_complete_ts AS test_complete_ts,
  ap.platform,
  ap.first_open_ts,
  ap.paired_ts,
  (ap.paired_ts IS NOT NULL) AS is_paired,
  ap.d30_active,
  ap.censored
FROM silver.v_attribution_eligible e
JOIN silver.web_user_stages w ON w.user_pseudo_id = e.web_pseudo_id
JOIN silver.app_user_stages ap ON ap.hashed_device_id = e.app_device_id
WHERE w.first_test_complete_ts IS NOT NULL
  AND ap.first_open_ts > w.first_test_complete_ts;

COMMENT ON TABLE silver.linked_journeys IS
  'One row per bridge-linked (silver.v_attribution_eligible), temporally-ordered web-to-app journey: hearing_test_complete followed by a LATER first app_open. Carries acquisition_channel (first-touch, from the identity bridge) and last_touch_campaign (last-touch, from web) side by side. Caveat: this is the linkable population only (consented + signed-in + completed-before-opening) — treat downstream counts as a lower bound / channel-mix signal, never as total volume.';

-- ============================================================================
-- GOLD — business marts. One table per KPI the agent serves; every KPI in
-- metrics.yaml is now a thin SELECT over one of these.
-- ============================================================================

CREATE OR REPLACE TABLE gold.funnel_overview AS
WITH web_start AS (
  SELECT COUNT(*) AS users FROM silver.web_user_stages WHERE first_test_start_ts IS NOT NULL
),
web_complete AS (
  SELECT COUNT(*) AS users FROM silver.web_user_stages WHERE first_test_complete_ts IS NOT NULL
),
app_download AS (
  SELECT COUNT(*) AS users FROM silver.app_user_stages
),
app_paired AS (
  SELECT COUNT(*) AS users FROM silver.app_user_stages WHERE paired_ts IS NOT NULL
),
d30_active AS (
  SELECT COUNT(*) AS users FROM silver.app_user_stages WHERE d30_active AND NOT censored
)
SELECT 1 AS stage_order, 'hearing_test_start' AS stage, users FROM web_start
UNION ALL
SELECT 2, 'hearing_test_complete', users FROM web_complete
UNION ALL
SELECT 3, 'app_download', users FROM app_download
UNION ALL
SELECT 4, 'hearing_aid_paired', users FROM app_paired
UNION ALL
SELECT 5, 'active_d30', users FROM d30_active
ORDER BY stage_order;

COMMENT ON TABLE gold.funnel_overview IS
  'Distinct users/devices at each of the 5 funnel stages (hearing_test_start/complete on web grain, app_download/hearing_aid_paired/active_d30 on app grain). Grain: one row per stage. Caveat: stages are counted per platform independently — they are NOT chained through a single cross-device user (see silver.linked_journeys for that).';

CREATE OR REPLACE TABLE gold.step_conversion AS
SELECT
  stage_order,
  LAG(stage) OVER (ORDER BY stage_order) || ' -> ' || stage AS step,
  LAG(users) OVER (ORDER BY stage_order) AS from_users,
  users AS to_users,
  1.0 * users / NULLIF(LAG(users) OVER (ORDER BY stage_order), 0) AS conversion_rate
FROM gold.funnel_overview;

COMMENT ON TABLE gold.step_conversion IS
  'Step-to-step conversion rate between consecutive gold.funnel_overview stages. Grain: one row per stage (the first stage has NULL from_users/conversion_rate — callers filter it out). Caveat: the complete->download step mixes web pseudo ids and hashed app devices; the rate is a population-level estimate, not a per-user join.';

CREATE OR REPLACE TABLE gold.completion_by_channel AS
SELECT
  last_touch_campaign AS channel,
  COUNT(*) AS test_starts,
  SUM(CASE WHEN first_test_complete_ts IS NOT NULL THEN 1 ELSE 0 END) AS test_completes,
  1.0 * SUM(CASE WHEN first_test_complete_ts IS NOT NULL THEN 1 ELSE 0 END)
    / NULLIF(COUNT(*), 0) AS completion_rate
FROM silver.web_user_stages
WHERE first_test_start_ts IS NOT NULL
GROUP BY last_touch_campaign
ORDER BY completion_rate DESC;

COMMENT ON TABLE gold.completion_by_channel IS
  'Hearing-test completion rate by last-touch UTM channel, web grain only. Grain: one row per channel. Caveat: sessions without a campaign are grouped as organic/none; web pseudonymous ids are used, so consent-denied users are still visible at this aggregated level.';

CREATE OR REPLACE TABLE gold.completion_by_device AS
SELECT
  device_category,
  COUNT(*) AS test_starts,
  SUM(CASE WHEN first_test_complete_ts IS NOT NULL THEN 1 ELSE 0 END) AS test_completes,
  1.0 * SUM(CASE WHEN first_test_complete_ts IS NOT NULL THEN 1 ELSE 0 END)
    / NULLIF(COUNT(*), 0) AS completion_rate
FROM silver.web_user_stages
WHERE first_test_start_ts IS NOT NULL
GROUP BY device_category
ORDER BY completion_rate DESC;

COMMENT ON TABLE gold.completion_by_device IS
  'Hearing-test completion rate by web device_category (desktop/mobile/tablet). Grain: one row per device category. Caveat: web grain only; device_category is stable per user in this dataset, so this equals a per-user grouping.';

CREATE OR REPLACE TABLE gold.downloads_by_channel AS
SELECT
  acquisition_channel,
  COUNT(DISTINCT hashed_id) AS linked_downloads
FROM silver.linked_journeys
GROUP BY acquisition_channel
ORDER BY linked_downloads DESC;

COMMENT ON TABLE gold.downloads_by_channel IS
  'Bridge-linked app downloads (first app_open AFTER hearing_test_complete) by first-touch acquisition_channel. Grain: one row per channel. Caveat: only consented, signed-in, linkable users appear — treat as a lower bound and a channel-mix signal, not absolute download volume.';

CREATE OR REPLACE TABLE gold.pairing_by_channel AS
SELECT
  acquisition_channel,
  COUNT(DISTINCT hashed_id) AS linked_downloads,
  SUM(CASE WHEN is_paired THEN 1 ELSE 0 END) AS paired_users,
  1.0 * SUM(CASE WHEN is_paired THEN 1 ELSE 0 END)
    / NULLIF(COUNT(DISTINCT hashed_id), 0) AS pairing_rate
FROM silver.linked_journeys
GROUP BY acquisition_channel
ORDER BY pairing_rate DESC;

COMMENT ON TABLE gold.pairing_by_channel IS
  'Among bridge-linked downloads (silver.linked_journeys), the share that paired a hearing aid, by first-touch acquisition_channel. Grain: one row per channel. Caveat: restricted to consented, signed-in users; channels whose users consent less often are under-represented.';

CREATE OR REPLACE TABLE gold.pairing_by_platform_market AS
SELECT
  e.market,
  ap.platform,
  COUNT(DISTINCT ap.hashed_device_id) AS app_devices,
  SUM(CASE WHEN ap.paired_ts IS NOT NULL THEN 1 ELSE 0 END) AS paired_devices,
  1.0 * SUM(CASE WHEN ap.paired_ts IS NOT NULL THEN 1 ELSE 0 END)
    / NULLIF(COUNT(DISTINCT ap.hashed_device_id), 0) AS pairing_rate
FROM silver.app_user_stages ap
JOIN silver.v_attribution_eligible e ON e.app_device_id = ap.hashed_device_id
GROUP BY e.market, ap.platform
ORDER BY e.market, ap.platform;

COMMENT ON TABLE gold.pairing_by_platform_market IS
  'Among bridge-linked app devices with at least one app_open, the pairing share by market and platform (iOS/Android). Grain: one row per market x platform. Caveat: deliberately NOT restricted by the linked_journeys temporal-order rule — this is every linked device that opened the app, regardless of whether a web test was completed first; market is only known through the identity bridge, so this covers the linkable population, not every app device.';

CREATE OR REPLACE TABLE gold.d30_by_channel AS
SELECT
  e.acquisition_channel,
  COUNT(DISTINCT ap.hashed_device_id) AS eligible_users,
  SUM(CASE WHEN ap.d30_active THEN 1 ELSE 0 END) AS retained_users,
  1.0 * SUM(CASE WHEN ap.d30_active THEN 1 ELSE 0 END)
    / NULLIF(COUNT(DISTINCT ap.hashed_device_id), 0) AS d30_retention_rate
FROM silver.app_user_stages ap
JOIN silver.v_attribution_eligible e ON e.app_device_id = ap.hashed_device_id
WHERE NOT ap.censored
GROUP BY e.acquisition_channel
ORDER BY d30_retention_rate DESC;

COMMENT ON TABLE gold.d30_by_channel IS
  'D30 retention (active 28-34 days after first open) among bridge-linked, non-censored app devices, by first-touch acquisition_channel. Grain: one row per channel. Caveat: devices whose first_open is within 34 days of the data horizon are excluded as right-censored; retention of non-linkable users is unobservable by design.';

CREATE OR REPLACE TABLE gold.weekly_test_starts AS
SELECT
  DATE_TRUNC('week', first_test_start_ts) AS week_start,
  COUNT(*) AS test_starts
FROM silver.web_user_stages
WHERE first_test_start_ts IS NOT NULL
GROUP BY DATE_TRUNC('week', first_test_start_ts)
ORDER BY week_start;

COMMENT ON TABLE gold.weekly_test_starts IS
  'Distinct web users starting the hearing test per calendar week (week starting Monday). Grain: one row per week. Caveat: weeks at the edges of the data window are partial.';

CREATE OR REPLACE TABLE gold.linkable_share_by_market AS
WITH redirects AS (
  SELECT market, COUNT(*) AS redirect_users
  FROM silver.web_user_stages
  WHERE app_store_redirect_ts IS NOT NULL
  GROUP BY market
),
bridged AS (
  SELECT market, COUNT(DISTINCT hashed_id) AS bridge_users
  FROM silver.v_attribution_eligible
  GROUP BY market
)
SELECT
  r.market,
  r.redirect_users,
  COALESCE(b.bridge_users, 0) AS bridge_users,
  1.0 * COALESCE(b.bridge_users, 0) / NULLIF(r.redirect_users, 0) AS linkable_share
FROM redirects r
LEFT JOIN bridged b ON b.market = r.market
ORDER BY linkable_share DESC;

COMMENT ON TABLE gold.linkable_share_by_market IS
  'How much of the app-download population is linkable across web and app per market: bridge-eligible users divided by web users who hit the app-store redirect in that market. Grain: one row per market. Caveat: a deliberate approximation — true downloaders per market are unknowable (app events carry no market), so web app_store_redirect users stand in as the denominator; read as "share of the funnel measurable end-to-end", not a precise link rate.';

-- gold.attribution_first_vs_last — module M4a: attributes the SAME
-- linkable-downloader population twice, once per attribution model, so the
-- two models can be compared apples-to-apples (see docs/knowledge/attribution.md).
-- utm_campaign -> channel mapping mirrors id_bridge.acquisition_channel's
-- channel vocabulary: summer_hearing_meta -> paid_social_meta,
-- tiktok_awareness -> paid_social_tiktok, brand_search -> paid_search_brand,
-- retargeting_meta -> retargeting_meta, NULL (i.e. silver.web_user_stages'
-- 'organic/none' placeholder, or any unmapped value) -> organic_direct.
CREATE OR REPLACE TABLE gold.attribution_first_vs_last AS
WITH downloaders AS (
  -- One row per linkable downloader (silver.linked_journeys grain), carrying
  -- both attribution models side by side before either is aggregated.
  SELECT
    hashed_id,
    acquisition_channel AS first_touch_channel,
    CASE last_touch_campaign
      WHEN 'summer_hearing_meta' THEN 'paid_social_meta'
      WHEN 'tiktok_awareness'    THEN 'paid_social_tiktok'
      WHEN 'brand_search'        THEN 'paid_search_brand'
      WHEN 'retargeting_meta'    THEN 'retargeting_meta'
      ELSE 'organic_direct'
    END AS last_touch_channel
  FROM silver.linked_journeys
),
first_touch AS (
  SELECT first_touch_channel AS channel, COUNT(DISTINCT hashed_id) AS attributed_downloads
  FROM downloaders
  GROUP BY first_touch_channel
),
last_touch AS (
  SELECT last_touch_channel AS channel, COUNT(DISTINCT hashed_id) AS attributed_downloads
  FROM downloaders
  GROUP BY last_touch_channel
)
SELECT channel, 'first_touch' AS attribution_model, attributed_downloads FROM first_touch
UNION ALL
SELECT channel, 'last_touch' AS attribution_model, attributed_downloads FROM last_touch
ORDER BY channel, attribution_model;

COMMENT ON TABLE gold.attribution_first_vs_last IS
  'The linkable-downloader population (silver.linked_journeys) attributed TWICE: once to its CRM first-touch channel (acquisition_channel) and once to its last-touch channel (web last_touch_campaign, mapped to the same channel vocabulary as acquisition_channel). Grain: one row per channel x attribution_model. Invariant: SUM(attributed_downloads) is identical across the two attribution_model values, because it is the same population counted twice. Caveat: linkable population only (consented + signed-in + bridge-linked downloaders) — never total download volume; where the two models disagree on a channel''s share, first-touch reflects demand creation (who was acquired) and last-touch reflects closing (what session finished the test), and the disagreement itself is the point of this mart, not noise to be reconciled away.';

-- gold.completion_by_channel_device — module M4a: the H1 (traffic quality)
-- vs H2 (mobile UX) falsification cross-tab. Both hypotheses predict lower
-- completion for paid-social channels, because paid-social traffic also
-- skews mobile in this dataset (a real confound) — only a channel x device
-- grid, not either dimension alone, can tell them apart. All web users who
-- started the test (no bridge/consent restriction), unlike the
-- linkable-only attribution mart above.
CREATE OR REPLACE TABLE gold.completion_by_channel_device AS
SELECT
  CASE last_touch_campaign
    WHEN 'summer_hearing_meta' THEN 'paid_social_meta'
    WHEN 'tiktok_awareness'    THEN 'paid_social_tiktok'
    WHEN 'brand_search'        THEN 'paid_search_brand'
    WHEN 'retargeting_meta'    THEN 'retargeting_meta'
    ELSE 'organic_direct'
  END AS channel,
  device_category,
  COUNT(*) AS users_started,
  SUM(CASE WHEN first_test_complete_ts IS NOT NULL THEN 1 ELSE 0 END) AS users_completed,
  1.0 * SUM(CASE WHEN first_test_complete_ts IS NOT NULL THEN 1 ELSE 0 END)
    / NULLIF(COUNT(*), 0) AS completion_rate
FROM silver.web_user_stages
WHERE first_test_start_ts IS NOT NULL
GROUP BY 1, 2
ORDER BY channel, device_category;

COMMENT ON TABLE gold.completion_by_channel_device IS
  'Hearing-test completion rate by last-touch channel AND web device_category together. Grain: one row per channel x device_category. Caveat: web grain, ALL web users who started the test (not restricted to the linkable/bridge population); channel uses the same utm_campaign -> channel mapping as gold.attribution_first_vs_last''s last-touch model. Built specifically so the traffic-quality hypothesis (a channel is weak everywhere) and the mobile-UX hypothesis (mobile is weak everywhere) can be told apart from a channel-only or device-only mixture of the two: compare completion_rate across device_category WITHIN one channel, not just across channels.';

-- ============================================================================
-- M11 — two DAILY, DIMENSIONAL gold cubes backing the natural-language
-- "build me a KPI dashboard for <range> for <market>" flow
-- (config/dashboard_kpis.json + agent.dashboard). Where every gold mart
-- above is a single pre-aggregated shape for one KPI, these two are cut by
-- every dimension a dashboard filter chip can name (day x market x device
-- x channel, and day x channel x market x platform) so one filtered
-- SELECT ... WHERE {{where}} GROUP BY ... can serve any combination of
-- filters without a bespoke mart per filter combination — the "promote a
-- frequently-asked filtered question to dimensional gold" pattern (see
-- docs/knowledge/methodology.md).
--
-- M11-fix: originally shipped WEEK-grained (week_start). A real run of
-- "the last 3 DAYS for Germany" silently produced a 6-week window instead,
-- because a week-grain cube cannot answer a day-level filter and the LLM
-- quietly improvised a supported range rather than refusing. Re-grained to
-- DAILY (day_date) — the finest question this dashboard can plausibly be
-- asked ("last N days" is a phrase people use; the funnel has no
-- meaningful hour-level filter) — with any coarser rollup (week, month)
-- left as a GROUP BY on the caller's side, never baked into the cube.
--
-- PARITY IS SACRED: with no WHERE filter (WHERE 1=1), summing either cube
-- over every dimension reproduces the existing, unfiltered gold marts'
-- totals byte-for-byte (see tests/test_dashboard_cubes.py) — these cubes
-- are a re-cut of the exact same silver rows the marts above already read,
-- never a new computation. A day_date cube grouped back up to ISO weeks
-- also reproduces the retired week_start cube's per-week totals exactly
-- (also asserted in tests/test_dashboard_cubes.py).
-- ============================================================================

-- gold.web_funnel_daily_cube: the web side of the funnel (test start ->
-- complete -> app-store redirect), cut by the DAY each user FIRST reached
-- that particular stage (consistent with every other gold mart's
-- MIN/first-event rule), plus market, device_category and channel. Built
-- as three independently-bucketed measures (a user's start/complete/
-- redirect days can differ) UNION'd into one dimension key set and then
-- LEFT JOINed back so every (day, market, device, channel) combination
-- that has ANY of the three measures gets one row with the other two
-- COALESCEd to 0, rather than three separate row sets.
--
-- M11-fix (grain change, weekly -> daily): a real run asked "the last 3
-- DAYS for Germany" and got a silent "last 6 weeks" instead, because the
-- original cube's week_start grain made a day-level question structurally
-- unanswerable and the LLM improvised. DAILY is chosen as the finest grain
-- a user can plausibly ask a filter question at ("last N days" is a real
-- phrase; "last N hours" is not, for a funnel of this nature) -- any
-- coarser bucket (week, month) a caller wants is a GROUP BY on top of this
-- cube (see dash_weekly_test_starts in config/dashboard_kpis.json, which
-- now buckets day_date up to weeks itself), never the other way around.
CREATE OR REPLACE TABLE gold.web_funnel_daily_cube AS
WITH mapped AS (
  -- channel uses the SAME last_touch_campaign -> channel vocabulary as
  -- gold.completion_by_channel_device / gold.attribution_first_vs_last's
  -- last-touch model (not gold.completion_by_channel's raw utm string) —
  -- a deliberate M11 choice so one `channel` filter value (e.g.
  -- 'paid_social_meta') means the same thing on this cube AND on
  -- gold.journey_daily_cube below, which only ever carries the bridge's
  -- acquisition_channel vocabulary. Parity with gold.completion_by_channel
  -- therefore holds in TOTAL (see tests/test_dashboard_cubes.py), not
  -- per-legacy-channel-label.
  SELECT
    market,
    device_category,
    CASE last_touch_campaign
      WHEN 'summer_hearing_meta' THEN 'paid_social_meta'
      WHEN 'tiktok_awareness'    THEN 'paid_social_tiktok'
      WHEN 'brand_search'        THEN 'paid_search_brand'
      WHEN 'retargeting_meta'    THEN 'retargeting_meta'
      ELSE 'organic_direct'
    END AS channel,
    first_test_start_ts,
    first_test_complete_ts,
    app_store_redirect_ts
  FROM silver.web_user_stages
),
starts AS (
  SELECT CAST(first_test_start_ts AS DATE) AS day_date,
         market, device_category, channel, COUNT(*) AS test_starts
  FROM mapped
  WHERE first_test_start_ts IS NOT NULL
  GROUP BY 1, 2, 3, 4
),
completes AS (
  SELECT CAST(first_test_complete_ts AS DATE) AS day_date,
         market, device_category, channel, COUNT(*) AS test_completes
  FROM mapped
  WHERE first_test_complete_ts IS NOT NULL
  GROUP BY 1, 2, 3, 4
),
redirects AS (
  SELECT CAST(app_store_redirect_ts AS DATE) AS day_date,
         market, device_category, channel, COUNT(*) AS store_redirects
  FROM mapped
  WHERE app_store_redirect_ts IS NOT NULL
  GROUP BY 1, 2, 3, 4
),
dims AS (
  SELECT day_date, market, device_category, channel FROM starts
  UNION
  SELECT day_date, market, device_category, channel FROM completes
  UNION
  SELECT day_date, market, device_category, channel FROM redirects
)
SELECT
  d.day_date,
  d.market,
  d.device_category,
  d.channel,
  COALESCE(s.test_starts, 0) AS test_starts,
  COALESCE(c.test_completes, 0) AS test_completes,
  COALESCE(r.store_redirects, 0) AS store_redirects
FROM dims d
LEFT JOIN starts s USING (day_date, market, device_category, channel)
LEFT JOIN completes c USING (day_date, market, device_category, channel)
LEFT JOIN redirects r USING (day_date, market, device_category, channel)
ORDER BY day_date, market, device_category, channel;

COMMENT ON TABLE gold.web_funnel_daily_cube IS
  'Daily, dimensional web-funnel cube. Grain: one row per day_date x market x device_category x channel. Grain chosen as the finest question a user can plausibly ask this filterable dashboard ("the last 3 days"): any coarser bucket a caller wants (week, month) is a GROUP BY on top of this cube, never the reverse -- a week-grain cube cannot answer a day-level filter (see M11-fix). Measures: test_starts/test_completes/store_redirects, each a user counted on the DAY of THEIR OWN first occurrence of that stage (a user''s start/complete/redirect days can differ; each measure is bucketed independently, then combined, so a cell with e.g. test_completes>0 and test_starts=0 just means no one in that exact day+market+device+channel combination started AND completed on the same day). channel is the mapped vocabulary (paid_social_meta, paid_search_brand, paid_social_tiktok, retargeting_meta, organic_direct) shared with gold.journey_daily_cube, NOT gold.completion_by_channel''s raw utm string. Source: silver.web_user_stages (web grain, no bridge/consent restriction). Caveat: with no filter, SUM(test_starts)/SUM(test_completes) over the whole cube reproduce gold.funnel_overview''s hearing_test_start/hearing_test_complete totals exactly; per-channel splits will not match gold.completion_by_channel''s raw-utm channel labels (see channel note above); grouping day_date up to ISO weeks (DATE_TRUNC(''week'', day_date)) reproduces the retired weekly cube''s per-week totals exactly (see tests/test_dashboard_cubes.py).';

-- gold.journey_daily_cube: the bridge-linked, consented web-to-app journey
-- (silver.linked_journeys), cut by the DAY of first app open ("download
-- day"), acquisition_channel, market and platform. See the M11-fix note on
-- gold.web_funnel_daily_cube above for why this is daily, not weekly.
CREATE OR REPLACE TABLE gold.journey_daily_cube AS
SELECT
  CAST(first_open_ts AS DATE) AS day_date,
  acquisition_channel,
  market,
  platform,
  COUNT(DISTINCT hashed_id) AS downloads,
  SUM(CASE WHEN is_paired THEN 1 ELSE 0 END) AS paired_users,
  -- d30_eligible: downloads in this cell whose D30 window has actually
  -- closed (NOT censored) -- the correct denominator for a d30 RATE, not
  -- `downloads` itself (a very recent day's downloads are almost all
  -- still censored and would otherwise silently understate retention, the
  -- exact bug silver.app_user_stages.censored exists to prevent -- see
  -- docs/knowledge/methodology.md's D30 section). Added beyond the
  -- downloads/paired_users/d30_retained measure list because a rate needs
  -- both a numerator AND a valid denominator to reproduce the existing
  -- d30 mart's censoring-aware semantics; d30_retained alone cannot be
  -- turned into an honest rate against `downloads`.
  SUM(CASE WHEN NOT censored THEN 1 ELSE 0 END) AS d30_eligible,
  SUM(CASE WHEN NOT censored AND d30_active THEN 1 ELSE 0 END) AS d30_retained
FROM silver.linked_journeys
GROUP BY 1, 2, 3, 4
ORDER BY day_date, acquisition_channel, market, platform;

COMMENT ON TABLE gold.journey_daily_cube IS
  'Daily, dimensional bridge-linked journey cube. Grain: one row per day_date (day of first app open, i.e. "download day") x acquisition_channel x market x platform. Grain chosen as the finest question a user can plausibly ask this filterable dashboard ("the last 3 days"); any coarser bucket a caller wants is a GROUP BY on top of this cube, never the reverse (see M11-fix). Measures: downloads (distinct bridge-linked users whose first app_open followed their web test completion), paired_users (of those, how many paired a hearing aid), d30_eligible (of those, how many are NOT right-censored -- their 28-34 day D30 window has closed) and d30_retained (of the eligible ones, how many were actually active in that window); a D30 rate must divide d30_retained by d30_eligible, never by downloads. Source: silver.linked_journeys -- CONSENTED, SIGNED-IN, BRIDGE-LINKED USERS ONLY, temporally ordered (app open strictly after web completion); treat every number here as the linkable population, never total volume. Caveat: with no filter, SUM(downloads)/SUM(paired_users)/SUM(d30_eligible)/SUM(d30_retained) over the whole cube reproduce gold.downloads_by_channel / gold.pairing_by_channel / gold.d30_by_channel''s totals exactly on THIS dataset (see tests/test_dashboard_cubes.py) -- but note that linked_journeys is, by construction, the temporally-ordered subset of the eligible x app-open population gold.d30_by_channel/gold.pairing_by_platform_market join directly (app open strictly after test completion), so a future dataset where that temporal rule actually excludes rows would make this cube''s totals a strict subset of theirs, not a guaranteed match. Grouping day_date up to ISO weeks (DATE_TRUNC(''week'', day_date)) reproduces the retired weekly cube''s per-week totals exactly (see tests/test_dashboard_cubes.py).';
