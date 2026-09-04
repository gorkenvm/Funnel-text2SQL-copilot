# Ask-the-Funnel Agent (module M1)

A small, guarded text-to-insight agent over a privacy-aware hearing-test
funnel: web events -> app events, linkable across devices **only** through a
consent-based identity bridge.

## Architecture

```
question ──> llm.py (planner)          metrics.yaml (10-KPI registry,
                │   mode=metric ────────>   thin SELECT per gold mart)
                │   mode=sql ────> guardrails.py (validate + LIMIT)
                │   mode=clarify ─> polite follow-up
                ▼
           agent.py (FunnelAgent) ──> db.py (DuckDB / Databricks driver)
                │                          │
                │                          ▼
                │                    medallion.py: applies sql/medallion.sql
                │                    (bronze -> silver -> gold, ONE file,
                │                     identical on both engines)
                ▼
   {question, mode, sql, rows, chart, answer, consent_note}
```

Since module M3c, the funnel/attribution/D30 SQL itself lives in exactly
one place — `sql/medallion.sql`, a versioned, ANSI-flavoured SQL file — and
is applied identically by both drivers via `agent.medallion.apply_medallion`.
`metrics.yaml`'s 10 KPIs are now thin `SELECT`s over the resulting
`gold.*` marts; ad-hoc agent SQL (`run_sql`) may reference the legacy bare
table names or any qualified `bronze.*`/`silver.*`/`gold.*` object. See
`docs/deploy_guide.md` section 6 for the full bronze/silver/gold reference
(including a diagram) and `sql/medallion.sql` itself for every object's
`COMMENT ON` (purpose, grain, caveat).

| File               | Responsibility |
|--------------------|----------------|
| `db.py`            | `BaseDriver.query(sql) -> DataFrame`; `DuckDBDriver` registers the three parquet files as views (`web_events`, `app_events`, `id_bridge` — never the ground truth) then applies the medallion layer; `DatabricksDriver` targets a SQL warehouse (lazy import). `get_driver()` factory. |
| `medallion.py`     | `apply_medallion(execute_fn, raw_schema)`: parses/templates/executes `sql/medallion.sql` in order (COMMENT ON failures logged-and-skipped, everything else fatal). Also the canonical `BRONZE_TABLES`/`SILVER_TABLES`/`GOLD_TABLES` inventory `guardrails.py` and `scripts/load_to_databricks.py` both import. |
| `guardrails.py`    | `validate_sql`: primary layer parses the query with `sqlglot` (AST-based — single read-only statement, no DDL/DML/command node anywhere in the tree, every table reference whitelisted, including inside CTEs/subqueries/joins); the original regex/token checks still run afterwards as cheap defense-in-depth (and alone, if `sqlglot` is not installed — logged warning, identical to pre-M7a behaviour). `enforce_limit` appends `LIMIT 5000` when absent, and — module M7a — caps an existing LIMIT above `max_rows` down to it via sqlglot. |
| `metrics.yaml`     | 10 governed KPIs: key, title, description, honest `consent_note`, a thin gold-mart-backed SQL SELECT, declarative chart spec (`bar`/`line`/`funnel`/`stat`). |
| `llm.py`           | `LLMClient.plan(question, schema_doc, metric_keys) -> plan dict`. `AnthropicLLM` (strict-JSON planning, lazy import) or deterministic `KeywordLLM` fallback. `get_llm(tier=None)` factory — module M7a: `tier` ("fast"/"balanced"/"max", default "max") resolves to a concrete `OpenAILLM` model via `resolve_tier_model()`/`config/model_tiers.json` (see the Environment variables table below); `OpenAILLM` instances are cached per resolved model name. |
| `memory.py`        | Module M7a: `ConversationMemory` — in-process, per-session store of the last 5 turns as a small structured summary only (question, tables touched, metric key, one-line truncated result) — never raw rows or a full answer. TTL 2h, LRU-capped at 200 sessions. `is_referential_question()` is the deterministic activation gate; `format_context_block()` renders stored turns into the "PRIOR CONTEXT" system-prompt block `agentic.py` injects. |
| `agent.py`         | `FunnelAgent.ask()` orchestrates plan -> guardrails -> execution -> plain-English answer; also `list_metrics()` / `run_metric(key)`. |
| `agentic.py`       | `AgenticFunnelAgent`: multi-step tool-use loop (get_schema/run_sql/search_knowledge/get_metric); system prompt tells the LLM to prefer gold marts for standard KPIs, silver for user-grain analysis, bronze for raw events, that row-level cross-device joins only go through `silver.v_attribution_eligible`, and (module M7a, "QA-5") to add a one-line consent/linkability caveat whenever the answer relies on `id_bridge`/`linked_journeys`/a linkable-only gold mart. `run()` optionally takes `session_id`/`memory_enabled` to steer/record `ConversationMemory`. |
| `demo.py`          | Eight canned English questions end-to-end. |

### Privacy / measurement design

- Web stages are counted on pseudonymous web ids, app stages on hashed
  device ids. The only lawful cross-device join is `silver.v_attribution_eligible`
  (`bronze.id_bridge` filtered to `opt_in_flag = true` — consented,
  signed-in users), and every bridge-based KPI says so in its `consent_note`
  and in that view's `COMMENT ON`.
- Where the funnel crosses web -> app via the bridge, temporal order is
  enforced in `silver.linked_journeys` (`first app_open > hearing_test_complete`).
- D30 retention: active if any `app_open` falls 28-34 days (inclusive)
  after the device's first open (`silver.app_user_stages.d30_active`);
  devices whose first open is within 34 days of the data horizon are
  excluded as right-censored (`silver.app_user_stages.censored`).
- The generator's `_ground_truth.parquet` is used **only** in tests.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `AGENT_DB` | `duckdb` | Driver: `duckdb` or `databricks`. |
| `AGENT_DATA_DIR` | `<repo>/data` | Location of the local parquet files. |
| `ANTHROPIC_API_KEY` | — | Enables `AnthropicLLM`; otherwise `KeywordLLM`. |
| `AGENT_MODEL` | `claude-sonnet-4-5` | **Legacy**, still honoured: pins a single model for `AnthropicLLM`, and — module M7a — overrides `OpenAILLM`'s model for *every* tier alike, winning over `config/model_tiers.json` and the per-tier env vars below. |
| `config/model_tiers.json` | `{"fast": "gpt-4o-mini", "balanced": {"model": "gpt-5.6-luna", "reasoning_effort": "none"}, "max": {"model": "gpt-5.6-terra", "reasoning_effort": "none"}}` | Module M7a (+ M12). `OpenAILLM`'s model (and, since M12, optional `reasoning_effort`) per tier (`get_llm(tier=...)`, default tier `"max"`). Each entry is a plain model-name string OR a `{"model", "reasoning_effort"}` object — `reasoning_effort` is one of `"none"`/`"low"`/`"medium"`/`"high"`, sent on every chat.completions call for that tier (needed for newer reasoning models — see `AGENT_REASONING_EFFORT` below and `docs/deploy_guide.md` §8.1's self-healing fallback if it's left unset). Edit this file's `"max"` entry to the strongest model your OpenAI account actually has access to before a real deployment. |
| `AGENT_MODEL_FAST` / `AGENT_MODEL_BALANCED` / `AGENT_MODEL_MAX` | — | Module M7a. Per-tier override of `config/model_tiers.json`'s model name (its `reasoning_effort`, if any, is kept), without editing the file (e.g. for a container that sets env vars but ships a read-only image). |
| `AGENT_REASONING_EFFORT` | — | Module M12. Blanket `reasoning_effort` for any tier that doesn't already name its own in `config/model_tiers.json` (a tier's explicit value always wins). |
| `DATABRICKS_SERVER_HOSTNAME` / `DATABRICKS_HTTP_PATH` / `DATABRICKS_TOKEN` | — | Databricks driver connection. |
| `DATABRICKS_CATALOG` | `workspace` | Databricks catalog the raw tables AND the bronze/silver/gold schemas live under. |
| `DATABRICKS_SCHEMA` | `funnel` | Databricks schema the three raw tables live under (this is the `{{raw}}` value `scripts/load_to_databricks.py` substitutes into `sql/medallion.sql`). |

## Run it

```bash
pip install -r requirements.txt

# demo (from the repo root)
PYTHONPATH=src python -m agent.demo

# tests
PYTHONPATH=src python -m pytest tests -q
```

M2 (web UI / chart rendering) builds on the declarative `chart` specs
returned by `ask()` — this module intentionally ships no server and no
plotting dependencies.
