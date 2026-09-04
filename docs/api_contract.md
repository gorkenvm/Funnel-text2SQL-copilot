# M2 API Contract

This is the frontend-facing contract for the Phonak Funnel Copilot web app
(module M2). **Any future frontend (web, mobile, embedded widget) should
depend only on this contract**, not on internals of `app/main.py` or the
M1 agent package.

Base URL: wherever the FastAPI app is deployed (e.g. `http://localhost:8000`,
`https://demo.vmgorken.com`). All request/response bodies are JSON.
Every error response — of any endpoint, any status code — has the shape:

```json
{ "error": "human-readable message" }
```

(422 validation errors additionally carry a `detail` array with per-field
information from FastAPI/pydantic.)

---

## `GET /health`

Liveness / status probe. No auth, no rate limit.

**Response 200**

```json
{
  "status": "ok",
  "driver": "duckdb",
  "llm": "keyword",
  "data_max_ts": "2026-08-24T00:00:00",
  "tiers": {"fast": "gpt-4o-mini", "balanced": "gpt-5.6-luna", "max": "gpt-5.6-terra"},
  "default_tier": "max",
  "locked": false
}
```

| Field | Type | Notes |
|---|---|---|
| `status` | string | Always `"ok"` if the process is up. |
| `driver` | `"duckdb" \| "databricks"` | Which `agent.db` driver is active. |
| `llm` | `"openai" \| "anthropic" \| "keyword"` | `"openai"`/`"anthropic"` only when the matching API key is configured and valid (see `agent.llm.get_llm()`'s resolution order); otherwise the deterministic fallback. |
| `data_max_ts` | string (ISO-8601) or `null` | Latest event timestamp across `web_events`/`app_events`; `null` if it could not be computed. |
| `tiers` | object | Module M7a. The concrete model name each of `fast`/`balanced`/`max` currently resolves to — see [Model tiers](#model-tiers-m7a) below. |
| `default_tier` | string | Module M7a. The tier used when a request's `tier` param is omitted (`"max"`). |
| `locked` | boolean | Module M13. `true` only when the deployment has `DEMO_PASSPHRASE` set (see [Demo gate](#demo-gate-m13) below) — never the passphrase itself. The frontend uses this to decide whether to show the lock screen before it has any key to try. |

---

## `POST /api/ask`

**Status: stable, legacy-compatible.** This endpoint's request/response
shape does not change with the M3a agentic upgrade below and will not
change in a breaking way going forward — it stays the simplest possible
"ask one question, get one JSON answer" contract for any client that does
not need a live trace. See `GET /api/ask/stream` for the richer,
multi-step, server-sent-events alternative.

Ask a natural-language question about the funnel.

**Request**

```json
{ "question": "Where is the biggest drop-off?", "lang": "en" }
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `question` | string | yes | 1–2000 chars. |
| `lang` | `"en" \| "tr"` | no (default `"en"`) | UI/narration language hint. See note below. |
| `tier` | `"fast" \| "balanced" \| "max"` | no (default `"max"`) | Module M7a. Which model answers this question — see [Model tiers](#model-tiers-m7a) below. Any other value is a 422. |
| `session_id` | string | no | Module M7a. 1–64 chars; sanitized server-side to `[A-Za-z0-9_.:-]` (other characters are stripped, never a 422). Identifies a conversation for [Conversation memory](#conversation-memory-m7a) below — omit it and memory plays no part at all. |
| `memory` | `"on" \| "off"` | no (default `"on"`) | Module M7a. `"off"` disables *injecting* stored context into this request (a `session_id` with history is still safe to send — it just will not be used to steer this answer). Any other value is a 422. |

**Response 200**

```json
{
  "question": "Where is the biggest drop-off?",
  "mode": "metric",
  "sql": "WITH web_start AS ( ... ) SELECT ...",
  "rows": [
    {"step": "hearing_test_start -> hearing_test_complete", "from_users": 100000, "to_users": 42062, "conversion_rate": 0.4206}
  ],
  "chart": {"type": "bar", "x": "step", "y": "conversion_rate"},
  "answer": "Step-to-step conversion rates. Highest conversion_rate: ... (4 groups).",
  "lang": "en"
}
```

| Field | Type | Notes |
|---|---|---|
| `question` | string | Echo of the input question. |
| `mode` | `"metric" \| "sql" \| "clarify" \| "refused" \| "error"` | How the agent answered. `rows`/`chart` are empty/`null` for `clarify`/`refused`/`error`. |
| `sql` | string or `null` | The SQL that was executed (or planned to be, for `refused`). |
| `rows` | array of objects | Up to 50 rows, JSON-safe (no NaN/Infinity/pandas Timestamps — timestamps are ISO-8601 strings, missing numbers are `null`). |
| `chart` | object or `null` | Declarative spec `{type, x, y, series?}` from M1's `metrics.yaml`; `type` is one of `bar`, `line`, `funnel`, `stat`. |
| `answer` | string | Plain-language narrative. |
| `lang` | `"en" \| "tr"` | Echo of the requested language. |

**Language note:** `lang: "tr"` only changes `answer` when the deployment has
`ANTHROPIC_API_KEY` configured (real LLM narration, best-effort translated to
Turkish). With the deterministic keyword planner (no API key), `answer`
always stays in English regardless of `lang` — only the UI chrome around it
is translated client-side.

**Errors**

| Status | When |
|---|---|
| 422 | `question` missing/empty/too long, `lang` not `en`/`tr`, `tier` not `fast`/`balanced`/`max`, or `memory` not `on`/`off`. |
| 429 | Per-IP rate limit exceeded (20 requests/minute by default). Bilingual message. |
| 500 | Unexpected server error (never includes a stack trace). |

---

## `GET /api/ask/stream` (M3a — agentic trace)

**Status: additive.** `POST /api/ask` above is unchanged and remains the
stable, legacy-compatible way to ask a question and get one JSON answer
back; this endpoint is a new, richer way to ask the same kind of question
and watch the agent work. A frontend can use either, or fall back from
one to the other (the reference UI does exactly that — see below).

Streams a live multi-step "agent trace" as
[Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events)
(`Content-Type: text/event-stream`) while answering a natural-language
question, then ends with a terminal `done` marker.

**Request**

```
GET /api/ask/stream?q=Where%20is%20the%20biggest%20drop-off%3F&lang=en
```

| Param | Type | Required | Notes |
|---|---|---|---|
| `q` | string | yes | 1–2000 chars. The question (same constraints as `POST /api/ask`'s `question`). |
| `lang` | `"en" \| "tr"` | no (default `"en"`) | Same meaning as `POST /api/ask`'s `lang`. |
| `tier` | `"fast" \| "balanced" \| "max"` | no (default `"max"`) | Module M7a — same meaning as `POST /api/ask`'s `tier`. |
| `session_id` | string | no | Module M7a — same meaning as `POST /api/ask`'s `session_id`. |
| `memory` | `"on" \| "off"` | no (default `"on"`) | Module M7a — same meaning as `POST /api/ask`'s `memory`. |
| `k` | string | only when the [demo gate](#demo-gate-m13) is on | The demo-gate key, as a query param — this endpoint's only fallback for `X-Demo-Key`, because `EventSource` cannot set request headers. Ignored (and unnecessary) when the gate is off. |

**Response 200** — a stream of frames, each either

```
data: {"type": "...", ...}\n\n
```

or the terminal marker

```
event: done\ndata: {}\n\n
```

which always closes the stream, after the final `answer` event (and,
on a fatal error, after error+answer both fired — the stream never ends
without an `answer`).

**Event types** (`type` is always present):

| `type` | Fields | When |
|---|---|---|
| `plan` | `text` | Always first: what the agent is about to do. |
| `context` | `text` | Module M7a. Emitted right after `plan`, ONLY when a `session_id` with stored history plus a referential question (`memory` not `"off"`) actually triggered a "PRIOR CONTEXT" injection into the system prompt — see [Conversation memory](#conversation-memory-m7a) below. Absent otherwise; a UI can show it as a small "using context from previous question" indicator. |
| `tool_call` | `name`, `arguments`, `call_id` | The agent is invoking a tool (`get_schema`, `run_sql`, `search_knowledge`, `get_metric`, or — module M11 — `build_dashboard`). |
| `tool_result` | `name`, `call_id`, `result`, `ok` | That tool's result (or `{"error": "..."}` with `ok: false`). |
| `sql` | `sql` | Emitted alongside a `run_sql` `tool_call`, so a UI can render the query being tried without waiting for the result. |
| `retry` | `attempt`, `error` | A `run_sql` failure the agent is about to self-correct (up to 2 retries; the failing SQL's error is fed back to the model). |
| `chart` | `chart` | Emitted after a successful `get_metric` call: the KPI's declarative chart spec. |
| `dashboard` | `cards`, `filter_label`, `applied_range`, `call_id` | Module M11 (+ M11-fix, + addendum 2). A natural-language dashboard/KPI-board request (e.g. "build me a KPI dashboard for the last 3 days for Germany") was recognized. `cards` is the same card shape as `POST /api/dashboard`'s response array (~10 filterable KPI cards from `config/dashboard_kpis.json`, sourced from `gold.web_funnel_daily_cube` / `gold.journey_daily_cube`), each including a `sql` field — the exact statement executed for that card. `filter_label` is a short human-readable summary of the resolved filters, e.g. `"Last 3 days · DE"`, or `"All data"` when nothing was filtered. `applied_range` (M11-fix) isolates the resolved date window from that compound string — `{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "label": "Last 3 days"}`, or `null` when no date filter is active — so a caller can echo exactly what range was used without re-parsing `filter_label`. `call_id` (addendum 2) matches the `build_dashboard` `tool_call`/`tool_result` pair's `call_id`, letting a UI attach these cards' SQL onto that same trace step; it is absent in keyless `KeywordLLM` mode, which never goes through the tool-call loop. Emitted either after a successful `build_dashboard` tool call (full `AgenticFunnelAgent` path) or, in keyless `KeywordLLM` mode, in place of the usual `plan`→`answer` pair whenever the question matches the dashboard intent. Always followed by a terminal `answer` event (a short paragraph that must name the applied `filter_label`, and say so explicitly if it differs from what was asked — see "M11-fix honesty rule" below) and then `done`. |
| `error` | `message` | A recoverable failure (planner error, retries exhausted, tool-call cap, or wall-clock timeout). Always followed by a best-effort `answer`. |
| `answer` | `answer`, `sql`, `rows`, `chart`, `citations` | Always the terminal content event. `citations` is `[{"source_file", "heading"}, ...]` from any `search_knowledge` calls made along the way (empty list if none). For a `dashboard`-mode answer, `sql`/`rows`/`chart` are `null`/`[]`/`null` — the cards already carry that data. |

**Which agent answers:** when the deployment's configured LLM supports
the multi-step tool-use loop (`OpenAILLM` or `AnthropicLLM` — i.e. a real
provider key is configured), the full `AgenticFunnelAgent` (module M3a)
runs and the trace can include any of the event types above. With the
deterministic keyless `KeywordLLM` (no API key configured — e.g. this
demo's default), the endpoint still streams a sensible two-event
sequence — `plan` then `answer` — from the M1/M2 single-shot
`FunnelAgent` path; in that mode `citations` is always `[]` and no
`tool_call`/`tool_result`/`sql`/`retry`/`chart` events are emitted.

**Errors**

| Status | When |
|---|---|
| 422 | `q` missing/empty/too long, `lang` not `en`/`tr`, `tier` not `fast`/`balanced`/`max`, or `memory` not `on`/`off`. |
| 429 | Per-IP rate limit exceeded (own limiter from `/api/ask`'s; 20 requests/minute by default). |

An error while already streaming never surfaces as an HTTP error status
(the response has already started with `200`): it becomes an `error`
event followed by a best-effort `answer` event, then `done`.

**Frontend fallback:** the reference UI opens an `EventSource` against
this endpoint and renders each event as a step in a live trace; if
`EventSource` itself errors (e.g. a proxy that does not support SSE), it
falls back to `POST /api/ask` and renders a normal single-shot answer.

---

## `GET /api/metrics`

List the 12 governed KPIs available for the dashboard.

**Response 200** — array of exactly 12 objects:

```json
[
  {
    "key": "funnel_overview",
    "title": "Funnel overview: users per stage",
    "description": "Distinct users at each funnel stage ...",
    "consent_note": "Web stages count pseudonymous web users ...",
    "chart": {"type": "funnel", "x": "stage", "y": "users"}
  }
]
```

---

## `POST /api/dashboard`

Run one or all registered KPIs and return ready-to-render cards. Module
M11 adds an optional `filters` field: when present (even `{}`), the
response is instead built from the ~10 **filterable** KPI templates in
`config/dashboard_kpis.json`, sourced from the two dimensional gold
cubes (`gold.web_funnel_daily_cube`, `gold.journey_daily_cube` — **day**
grain since the M11-fix, so a `date_start`/`date_end` pair even a day
apart is fully answerable) — see
[Filterable KPI dashboard (M11)](#filterable-kpi-dashboard-m11)
below. `keys` and `filters` are mutually exclusive selection modes;
when `filters` is omitted entirely, behavior is byte-for-byte the
pre-M11 legacy behavior described first. This endpoint takes explicit,
already-resolved dates — it never parses a relative-range phrase like
"last 3 days" itself (that only happens on the natural-language paths,
`POST /api/ask`/`GET /api/ask/stream`; see "M11-fix honesty rule" below).

**Request (legacy, unfiltered top-12)**

```json
{ "keys": ["funnel_overview", "weekly_test_starts_trend"] }
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `keys` | array of string or `null` | no | Omit or send `null`/`{}` to run all 12 registered KPIs. Ignored if `filters` is also present. |

**Response 200** — array of cards, one per requested key, in request order
(registry order when `keys` is omitted):

```json
[
  {
    "key": "funnel_overview",
    "title": "Funnel overview: users per stage",
    "consent_note": "Web stages count pseudonymous web users ...",
    "chart": {"type": "funnel", "x": "stage", "y": "users"},
    "rows": [{"stage_order": 1, "stage": "hearing_test_start", "users": 100000}],
    "answer": "Funnel overview: users per stage. hearing_test_start: 100,000 -> ...",
    "sql": "SELECT stage_order,\n       stage,\n       users\nFROM gold.funnel_overview\nORDER BY stage_order"
  }
]
```

`sql` (M11 addendum 2) is the exact statement executed for this card,
reindented for display via the shared `agent.sqlfmt.format_sql_for_display`
choke point — never the string that was actually run against the driver.

**Errors**

| Status | When |
|---|---|
| 400 | `keys` contains a key that is not in the registry. Message names the unknown key(s). |
| 500 | Unexpected server error. |

**Request (M11, filtered)**

```json
{
  "filters": {
    "date_start": "2026-06-01",
    "date_end": "2026-08-30",
    "market": "DE",
    "channel": null,
    "device": null,
    "platform": null
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `filters` | object or `null` | no | All six sub-fields are optional; send `{}` for "all data, but use the M11 filtered card set". `date_start`/`date_end` are `YYYY-MM-DD`. `market`/`channel`/`device`/`platform` must match a distinct value actually present in the cubes (case as stored — e.g. `"DE"`, not `"germany"`); the natural-language paths (chat, SSE) resolve friendly aliases like "Germany" before this validation runs. Unknown extra fields are rejected (`extra="forbid"`). |

**Response 200** — array of ~10 cards, one per filterable KPI template
in `config/dashboard_kpis.json`, same per-card shape as the legacy
response (`key`, `title`, `chart`, `rows`, `answer`, `sql`, optional
`consent_note`) plus no `keys`-style card count guarantee — the set is
fixed by the registry, not by request `keys`.

**Errors**

| Status | When |
|---|---|
| 422 | A `filters` field fails validation: `date_start` after `date_end`, a malformed date, or a `market`/`channel`/`device`/`platform` value not present in the data. The message names the offending field, e.g. `"Invalid filter 'market': ..."`. |
| 500 | Unexpected server error. |

---

## Filterable KPI dashboard (M11)

Lets a single natural-language command ("build me a KPI dashboard for
the last 3 days for Germany") or a direct `POST /api/dashboard`
call with a `filters` object produce a narrowed ~10-KPI dashboard,
instead of only the fixed unfiltered top-12.

**Two new dimensional gold cubes** (`sql/medallion.sql`), built
specifically to answer filtered dashboard questions without querying
raw silver tables per request:

- `gold.web_funnel_daily_cube` — grain: `day_date` × `market` ×
  `device_category` × `channel`, from `silver.web_user_stages`.
  Measures: `test_starts`, `test_completes`, `store_redirects`.
- `gold.journey_daily_cube` — grain: `day_date` ×
  `acquisition_channel` × `market` × `platform`, from
  `silver.linked_journeys`. Measures: `downloads`, `paired_users`,
  `d30_retained`, plus `d30_eligible` (the right-censoring-aware
  denominator — see the cube's `COMMENT ON`, since `d30_retained`
  alone cannot produce a correct rate).

**M11-fix — re-grained from week_start to day_date:** these cubes
originally used a `week_start` grain. A real run of "the last 3 days
for Germany" against that grain silently produced a "last 6 weeks"
dashboard instead — a week-grained cube structurally cannot answer a
day-level filter, so the (real-LLM) planner quietly substituted a
range it COULD express, with nothing telling the user a substitution
had happened. `day_date` is the finest grain a filter can plausibly be
asked at ("last N days" is a phrase people use; there is no meaningful
hour-level cut for this funnel); any coarser rollup a caller wants
(week, month) is a `GROUP BY` on top of the daily cube — see
`dash_weekly_test_starts` in `config/dashboard_kpis.json`, which now
buckets `day_date` up to ISO weeks itself rather than relying on the
cube already being week-grained. `tests/test_dashboard_cubes.py`
additionally proves a weekly rollup of the new daily cube reproduces
the retired weekly cube's per-week totals exactly, so the re-grain
changed nothing about the data — only what it can be filtered by.

Both cubes are unfiltered-total-parity-tested against the pre-existing
marts they generalize (`gold.funnel_overview`,
`gold.completion_by_channel`, `gold.downloads_by_channel`,
`gold.pairing_by_channel`, `gold.d30_by_channel`): summing a cube with
no `WHERE` clause reproduces those marts' totals exactly. This is the
"promote a frequently-asked filtered question to dimensional gold"
pattern — rather than adding one more single-purpose mart per new
filter combination, a small number of dimensional cubes cover the
whole filterable KPI set.

**KPI registry** (`config/dashboard_kpis.json`): ~10 templates (funnel
stages, step conversions, completion by channel/device/channel×device,
downloads by channel, pairing by channel/platform×market, d30 by
channel, weekly test-starts trend), each naming its source cube and a
`{{where}}` placeholder. A request's `filters` are never spliced into
SQL as raw text: each template also declares an allowlist mapping
generic filter names (`market`, `channel`, `device`, `platform`,
`date_start`, `date_end`) to actual cube columns, and only values
already confirmed present in the cube's distinct values (or a
validated date) are quoted into the `WHERE` clause server-side.

**Filters model**: `date_start`, `date_end`, `market`, `channel`,
`device`, `platform` — all optional. A relative phrase like "last 3
days"/"last 6 weeks"/"last 3 months" is resolved against the
**maximum event date actually present in the data**, never wall-clock
"today" — the demo dataset is static, so anchoring to today would
silently return an empty dashboard once the data ages past today minus
the window. Boundary semantics (uniform across all three units):
`date_start = horizon - N <unit>`, both ends inclusive — so "last 3
days" spans 4 calendar days inclusive of the horizon date, exactly as
"last 1 week" already spanned 8 days, not a clean 7; this keeps every
unit's arithmetic identical rather than special-casing days.

**M11-fix "honesty rule":** only days/weeks/months are supported units.
A relative-range phrase using anything else (e.g. "last 3 hours")
raises a structured, retryable error — `agent.dashboard.
UnparseableRangeError`, surfaced through the `build_dashboard` tool as
a failed `tool_result` naming the supported units — instead of the
request being silently dropped. The agentic system prompt additionally
requires the model's final answer to explicitly state the applied
`filter_label`, and say so explicitly when it differs from what the
user asked, so a range substitution (of any kind, not just the
unsupported-unit case) is never presented to the user as if it were
exactly what they requested.

---

## `GET /api/catalog` (M8)

The medallion table/view inventory backing the frontend's data-catalog side
panel. No request body/params. Fetched once by the client and cached in JS.

**Response 200**

```json
{
  "layers": {
    "bronze": [
      {"name": "bronze.web_events", "comment": "Raw web analytics events ..."},
      {"name": "bronze.app_events"},
      {"name": "bronze.id_bridge"}
    ],
    "silver": [
      {"name": "silver.web_user_stages"},
      {"name": "silver.app_user_stages"},
      {"name": "silver.v_attribution_eligible"},
      {"name": "silver.linked_journeys"}
    ],
    "gold": [
      {"name": "gold.funnel_overview"},
      {"name": "gold.step_conversion"}
    ]
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `layers.bronze` / `.silver` / `.gold` | array of objects | Every object in `agent.medallion.BRONZE_TABLES` / `SILVER_TABLES` / `GOLD_TABLES`, in that fixed order. `gold` has all 12 governed KPI marts plus (M11, day-grained since the M11-fix) the two filterable dimensional cubes (`web_funnel_daily_cube`, `journey_daily_cube`) — 14 objects. |
| `name` | string | Fully qualified `"<layer>.<object>"`, exactly as it may be referenced in SQL. |
| `comment` | string, optional | The object's `COMMENT ON` text, when cheaply available (DuckDB's own catalog introspection). Omitted rather than `null` when there is none — e.g. on a Databricks-backed deployment, or for an object with no comment. |

Row counts are intentionally not included — the M8 spec skips anything that
would need a query per object. Never errors: comment lookup is
best-effort and swallows its own failures.

---

## Model tiers (M7a)

Both ask endpoints accept an optional `tier`: `"fast"`, `"balanced"` or
`"max"` (default `"max"` — quality over cost is the deployment's default
choice). A tier only changes which model answers the question; nothing
else about the request/response contract changes. `GET /health`'s
`tiers` field reports which concrete model each tier currently resolves
to, and `default_tier` names the one used when a request omits `tier`.

Resolution (see `agent.llm.resolve_tier_model`/`resolve_tier_config`):
`config/model_tiers.json` ships each tier as either a plain model-name
string or, since module M12, a `{"model", "reasoning_effort"}` object —

```json
{
  "fast": "gpt-4o-mini",
  "balanced": {"model": "gpt-5.6-luna", "reasoning_effort": "none"},
  "max": {"model": "gpt-5.6-terra", "reasoning_effort": "none"}
}
```

— each tier is individually overridable by an `AGENT_MODEL_FAST` /
`AGENT_MODEL_BALANCED` / `AGENT_MODEL_MAX` environment variable (model
name only; a tier's configured `reasoning_effort` is kept). The legacy
`AGENT_MODEL` env var, if set, wins over all of that for every tier
alike (so a deployment that already pins a single model via
`AGENT_MODEL` keeps working unchanged, with every tier resolving to that
one model). `AGENT_REASONING_EFFORT` sets a blanket `reasoning_effort`
for any tier that doesn't already name its own. `reasoning_effort` is
sent on every OpenAI chat.completions call for that tier — needed for
newer reasoning models that 400 on `/v1/chat/completions` when function
tools and a non-`"none"` `reasoning_effort` are combined; `OpenAILLM`
also self-heals that exact 400 automatically even if this is left unset
(see `docs/deploy_guide.md` §8.1). Tiers only affect `OpenAILLM`;
`AnthropicLLM`/`KeywordLLM` deployments are unaffected by `tier` (and by
`config/model_tiers.json` entirely) — see `src/agent/README.md` and
`docs/deploy_guide.md` for the deployment-facing story on picking a
`"max"` model.

---

## Demo gate (M13)

An optional, lightweight passphrase gate for a public deployment — cost/
nuisance protection, **not** real authentication (no accounts, sessions,
or persistent brute-force protection). Entirely off, byte for byte
identical to every endpoint's contract above, unless the deployment sets
`DEMO_PASSPHRASE` (see `docs/deploy_guide.md` §9). When it is set, every
`/api/*` route — `POST /api/ask`, `GET /api/ask/stream`, `GET
/api/metrics`, `POST /api/dashboard`, `GET /api/catalog` — requires a
matching key. `GET /health` and the static frontend (`/`) are never
gated.

**Sending the key:** an `X-Demo-Key` request header, exact value the
candidate passphrase (the server does all normalization — trim, collapse
internal whitespace runs to one space, casefold — and a constant-time
comparison; the client should never normalize or guess at this itself).
`GET /api/ask/stream` additionally accepts the key as a `k` query param
(`?...&k=<key>`) — the one exception, because `EventSource` cannot set
request headers; every other endpoint only ever honors the header.

**On a missing or wrong key** — a static body, identical for every gated
endpoint, and the request never reaches the agent/driver/LLM at all:

```json
{ "error": "locked", "message": "This is a private demo. Enter the passphrase on the page to continue." }
```

- Status `401` for a missing/wrong key.
- Status `429`, after 5 consecutive failures from the same IP within a
  rolling 60-second lockout, with a retry-later `message` (in-memory
  per-IP counter — resets on a server restart).

`GET /health`'s `locked` field (see above) is how the frontend knows a
key is needed at all before it has one to try.

---

## Conversation memory (M7a)

Both ask endpoints accept two more optional params: `session_id` (a
client-chosen string identifying one conversation) and `memory`
(`"on"`/`"off"`, default `"on"`). Sending neither is a complete no-op —
the pre-M7a request/response shape is unchanged.

What gets stored, per session: only the last 5 turns, each a small
**structured** record — the question text, which tables its SQL touched
(if any), a registered metric key (if any), and the first line of the
answer (truncated to 160 characters). Never raw rows, never a full
answer. Sessions expire after 2 hours of inactivity, and at most 200
sessions are tracked at once (oldest evicted first) — see
`agent.memory.ConversationMemory`.

A **new** question is only steered by that history — a "PRIOR CONTEXT"
block added to the agent's system prompt, and the stream's `context`
event fired right after `plan` (see `GET /api/ask/stream`'s event table)
— when *all* of: `memory` is `"on"` (the default), `session_id` names a
session with stored turns, AND the new question itself looks referential
(matches a small EN/TR marker-word list — "that", "this", "bunu", "peki",
etc. — or is 5 words or fewer). An unrelated, self-contained question
never gets contaminated by old context, even with a populated session
history, because that last condition fails. `memory: "off"` disables
this injection outright, regardless of the other two conditions.

Every successfully-answered question is recorded into its `session_id`'s
history afterwards, independently of whether context was injected *into*
it — so the very first question in a session has nothing to draw on yet,
but is itself available as context for the next one.

This feature currently only steers the multi-step tool-use loop
(`GET /api/ask/stream` with a real, tool-calling LLM provider configured)
— `POST /api/ask`'s single-shot planner, and the stream's keyless
`KeywordLLM` fallback, have no system prompt to inject a context block
into, but both still *record* turns, so a session already has history by
the time a client with a real provider configured asks a follow-up.

---

## Chart spec reference (shared with M1)

`chart.type` is one of:

- `bar` — categorical bars; grouped by `series` when present (e.g. platform × market).
- `line` — time series (`x` is a period column).
- `funnel` — ordered stages (`x`); rendered as a horizontal bar in the reference UI.
- `stat` — single number (`rows[0][chart.y]`); rendered as a big-number tile.

A future frontend can render any chart type generically from `{type, x, y, series?}` + `rows` without knowing anything about the underlying SQL.
