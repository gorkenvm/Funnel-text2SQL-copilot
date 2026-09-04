# Sentinel Report — 2026-08-24

**DRAFT — pending analyst approval (human checkpoint)**

## Executive Summary

Sentinel run for 2026-08-24 found 9 finding(s): 3 critical, 2 warning, 4 info. Top critical item(s): hearing_aid_paired (app / Android) on 2026-08-24: actual=30 is 6.21σ below the trailing 28-day band (77.9 ± 7.7); download_to_pair rate on 2026-08-24: actual=14.0% (109/778) is 4.80σ below the trailing 28-day band (21.6% ± 1.6%); Expected event 'remote_support_session' has ZERO recorded rows in app_events on 2026-08-24 (registry expects it daily). Affected check(s): daily_event_volume, daily_funnel_rate, missing_event_today, schema_app_versions, schema_event_names. Recommended action: an analyst should review this DRAFT report before any distribution, per the human-checkpoint design.

## Findings (9)

### Critical (3)

- `daily_event_volume` **app:hearing_aid_paired:Android** — hearing_aid_paired (app / Android) on 2026-08-24: actual=30 is 6.21σ below the trailing 28-day band (77.9 ± 7.7).
- `daily_funnel_rate` **rate:download_to_pair** — download_to_pair rate on 2026-08-24: actual=14.0% (109/778) is 4.80σ below the trailing 28-day band (21.6% ± 1.6%).
- `missing_event_today` **missing_today:app_events.remote_support_session** — Expected event 'remote_support_session' has ZERO recorded rows in app_events on 2026-08-24 (registry expects it daily).

### Warning (2)

- `schema_app_versions` **app_version:7.0.0-beta** — New app_version '7.0.0-beta' observed in app_events, not in the registry.
- `schema_event_names` **event_name:app_events.device_paired** — New event_name 'device_paired' observed in app_events, not in the registry.

### Info (4)

- `daily_event_volume` **web:hearing_test_complete:US** — hearing_test_complete (web / US) on 2026-08-24: actual=147 is 1.66σ below the trailing 28-day band (167.9 ± 12.6).
- `daily_event_volume` **web:hearing_test_start:US** — hearing_test_start (web / US) on 2026-08-24: actual=403 is 1.60σ below the trailing 28-day band (437.4 ± 21.5).
- `daily_event_volume` **web:page_view:US** — page_view (web / US) on 2026-08-24: actual=362 is 1.67σ below the trailing 28-day band (396.5 ± 20.7).
- `daily_event_volume` **web:result_screen_view:US** — result_screen_view (web / US) on 2026-08-24: actual=147 is 1.69σ below the trailing 28-day band (167.9 ± 12.3).

## Schema Snapshot

- Columns inventoried: 21 across 3 table(s).
- Distinct event_name values observed: ['app_open', 'app_store_redirect', 'device_paired', 'hearing_aid_paired', 'hearing_test_complete', 'hearing_test_start', 'page_view', 'remote_support_session', 'result_screen_view']
- Distinct app_version values observed: ['6.2.1', '6.3.0', '6.3.1', '7.0.0-beta']

## Run Metadata

- as_of: 2026-08-24
- driver: DuckDBDriver (in-memory, tampered demo)
- exit_code: 2
- registry generated_at: 2026-09-02T13:24:52.107351+00:00
- thresholds: {"band_multiplier_info": 1.5, "band_multiplier_warning": 2.5, "band_multiplier_critical": 4.0, "min_volume_floor": 5, "min_history_days": 14}
- generated_at (this run, UTC): 2026-09-02T13:37:50.120683+00:00
