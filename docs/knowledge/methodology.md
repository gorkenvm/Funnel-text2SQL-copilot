# Funnel Methodology Notes

Background for anyone (human or agent) reasoning about the funnel KPIs:
what grain each stage is measured at, why some joins require a specific
temporal order, how the day-30 retention window is defined, and how a
support-interaction "recovery" signal should be read.

## Funnel grain: two identity spaces, one funnel

The funnel spans two systems with two different identifiers:

- Web stages (`hearing_test_start`, `hearing_test_complete`) are counted
  per **pseudonymous web id** (`user_pseudo_id`) — one browser/session
  identity, not a verified person.
- App stages (`app_open`, `hearing_aid_paired`) are counted per **hashed
  device id** (`hashed_device_id`) — one app installation, not a
  verified person either.

These two id spaces only overlap for users who are both **consented and
signed in**, captured in `id_bridge`. That means per-stage totals
(how many people started the test, how many opened the app) are each
individually honest, but they cannot be chained into a single
end-to-end "this exact person's journey" for the whole population —
only for the bridge-linked subset. Any KPI that reports one number per
stage without going through `id_bridge` is reporting two separate
populations side by side, not one cohort flowing through five stages.

## The temporal-order rule for web-to-app joins

Whenever a metric joins a bridge-linked user's web activity to their app
activity (for example, "downloads attributable to the funnel" or
"pairing rate among people who downloaded after testing"), the join
requires the app side to happen **strictly after** the web side:
`first_app_open > hearing_test_complete_timestamp`.

Without this rule, a user who already had the app installed for an
unrelated reason — and only later, coincidentally, completed the web
hearing test — would be miscounted as a funnel-driven download. Enforcing
temporal order keeps "download" and "pairing" metrics attributable to the
funnel rather than to pre-existing app usage.

## D30 retention: the 28-34 day window and right-censoring

"Day-30 retention" is not "active on exactly day 30 after first open."
It is operationalised as: **at least one `app_open` between 28 and 34
days (inclusive) after the user's first `app_open`.** Widening the point
estimate into a week-wide window absorbs normal weekly usage cadence
(someone who opens the app every 9 days would otherwise be misclassified
as "not retained" purely by bad luck of the calendar).

Right-censoring matters just as much as the window width: a user whose
first `app_open` happened fewer than 34 days before the data horizon
(the latest timestamp in the data) has not yet had the chance to reach
their day-28-34 window. Such users are **excluded from both the
numerator and the denominator** rather than counted as "not retained" —
counting them as churned would systematically understate retention for
every recently-acquired cohort, worst for the most recent weeks.

## The 14-day "recovered after support" signal

A complementary methodology note, not (yet) a registered KPI: among app
devices that have at least one `remote_support_session` event, the share
with a subsequent `app_open` **within 14 days** of that session is a
"recovered after support" rate. It is meant to flag whether a support
interaction is followed by renewed engagement (a good sign) or by
silence (a possible precursor to churn). Fourteen days is chosen the
same way the D30 window is: wide enough to absorb normal usage gaps,
narrow enough to still mean "shortly after," and the same right-censoring
logic applies — a support session too close to the data horizon has not
yet had its full 14-day window observed and should be excluded rather
than counted as "did not return."

See `insights.md` for why a high "recovered after support" rate should
not, by itself, be read as evidence that support *causes* retention.

## Filterable KPI dashboard cubes (M11): promoting a frequent question to dimensional gold

Every gold mart up to this point is a **single-purpose** aggregate:
one grain, no dimensions a caller can slice by beyond what is already
baked into the `GROUP BY` (e.g. `gold.completion_by_channel` is always
grouped by channel, never optionally by channel). That works well for
a fixed top-12 dashboard, but it cannot answer "show me the funnel for
Germany in the last 3 months" without either a bespoke mart per filter
combination or a query against silver on every request.

When one question shape (a filtered slice of the funnel/journey, by
date range, market, channel, device, or platform) is asked often
enough to be worth optimizing, the pattern used here is to **promote
it to a small dimensional gold cube**: one wider-grain aggregate,
carrying every dimension callers actually filter by, that a request
can `WHERE`-narrow at query time instead of needing a new mart per
combination. Two such cubes exist:

- `gold.web_funnel_daily_cube` — `day_date` × `market` ×
  `device_category` × `channel`, from `silver.web_user_stages`.
  Measures: `test_starts`, `test_completes`, `store_redirects`.
- `gold.journey_daily_cube` — `day_date` × `acquisition_channel` ×
  `market` × `platform`, from `silver.linked_journeys` (so the
  temporal-order rule above already applies to everything in it).
  Measures: `downloads`, `paired_users`, `d30_retained`, and
  `d30_eligible` — the same right-censoring logic from the D30 section
  above applies here too: `d30_retained` alone is not a rate without
  a denominator that excludes not-yet-eligible recent cohorts.

A cube is only trustworthy if summing it with no filter reproduces the
single-purpose marts it generalizes — both cubes are tested for exactly
that (unfiltered totals match `gold.funnel_overview`,
`gold.completion_by_channel`, `gold.downloads_by_channel`,
`gold.pairing_by_channel`, and `gold.d30_by_channel` exactly). One
side effect worth knowing about: `web_funnel_daily_cube.channel` and
`journey_daily_cube.acquisition_channel` deliberately share one mapped
channel vocabulary (`paid_social_meta`, `paid_search_brand`, etc.)
rather than each cube's own source table's raw UTM string, so that a
single `channel` filter value behaves identically against both cubes —
sums still match the legacy marts exactly, but a per-channel breakdown
from the cube will not look byte-identical to
`gold.completion_by_channel`'s raw-campaign-keyed rows.

### M11-fix: re-grained from weekly to daily

Both cubes originally used a `week_start` grain. A real run of "the
last 3 days for Germany" against that grain silently produced a
"last 6 weeks" dashboard instead: a week bucket cannot answer a
day-level question, and the (real-LLM) planner quietly substituted a
range it COULD express — with nothing surfaced to the user that a
substitution had even happened.

The fix re-cuts both cubes to `day_date` — the finest grain a filter
can plausibly be asked at for this dashboard ("last N days" is an
ordinary phrase; there is no meaningful hour-level cut for a hearing-
test funnel). The general principle: a dimensional gold cube's grain
should be the finest grain any supported filter can name, never
coarser — any rollup a caller wants (week, month) is a `GROUP BY` on
top of that finest grain, never the other way around. `gold.
web_funnel_daily_cube`'s own `dash_weekly_test_starts` KPI
(`config/dashboard_kpis.json`) is the concrete example: it still shows
a weekly trend, now by grouping `day_date` up to `DATE_TRUNC('week',
day_date)` itself, rather than depending on the cube already being
week-grained.

This re-grain changed nothing about the underlying numbers: grouping
the new daily cube back up to ISO weeks reproduces the retired weekly
cube's per-week totals exactly (see
`tests/test_dashboard_cubes.py::TestDailyCubeWeeklyRollupParity`) — it
only added the ability to filter finer than a week. Two things closed
the bug together, not one: the re-grain (so a day-level filter is
actually *answerable*), and a parser/tool-level "honesty rule" (so an
unsupported unit is a visible, retryable error rather than a silent
substitution) — see `docs/api_contract.md`'s "M11-fix honesty rule".
