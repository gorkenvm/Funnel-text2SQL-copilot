# System Architecture — Phonak Funnel Copilot

Two agents live in this system: an **interactive analytics agent** (answers
questions, live) and a **batch sentinel agent** (watches data health, daily).
They share the same warehouse, the same governance layer, and the same LLM
plumbing — but never call each other.

```mermaid
flowchart TB
    subgraph UI["Frontend — app/static/index.html (vanilla JS)"
        ]
        CHAT["Chat panel<br/>(SSE EventSource, live agent trace)"]
        DASH["Live dashboard<br/>(ECharts, top-12 KPI cards)"]
    end

    subgraph API["Backend — FastAPI (app/main.py)"]
        ASK["GET /api/ask/stream (SSE)<br/>POST /api/ask (legacy)"]
        DASHAPI["POST /api/dashboard<br/>GET /api/metrics · /health"]
        RL["per-IP rate limiters"]
    end

    subgraph AGENT["Interactive agent — AgenticFunnelAgent (agentic.py)"]
        LOOP["Hand-written tool-use loop<br/>max 6 tool calls · ~60s · SQL self-correction (2 retries)"]
    end

    subgraph TOOLS["Tool registry (tools.py)"]
        T1["get_schema<br/>(tables + samples, cached)"]
        T2["run_sql<br/>(read-only)"]
        T3["search_knowledge<br/>(RAG, k=3)"]
        T4["get_metric<br/>(registry KPI)"]
    end

    GUARD["Guardrails (guardrails.py)<br/>SELECT-only · table whitelist · forced LIMIT<br/>(planned: SQLGlot AST validation)"]
    REG["metrics.yaml<br/>12 KPIs → gold marts<br/>chart specs + consent notes"]
    KB["KnowledgeBase (knowledge.py)<br/>docs/knowledge/*.md<br/>OpenAI embeddings + disk cache<br/>BM25 fallback (offline)"]

    subgraph LLML["LLM layer (llm.py) — provider-agnostic chat_step"]
        L1["OpenAILLM<br/>gpt-4o-mini (default)"]
        L2["AnthropicLLM<br/>(optional)"]
        L3["KeywordLLM<br/>keyless fallback"]
        L4["ScriptedLLM<br/>(tests only)"]
    end

    subgraph DRV["Driver abstraction (db.py)"]
        D1["DuckDBDriver<br/>local parquet, dev/backup"]
        D2["DatabricksDriver<br/>SQL Warehouse, prod"]
    end

    subgraph LAKE["Lakehouse — sql/medallion.sql (one source, both engines)"]
        BR["bronze.*<br/>raw events"]
        SI["silver.*<br/>user-grain stages<br/><b>v_attribution_eligible</b> = consent gate"]
        GO["gold.*<br/>12 business marts"]
    end

    subgraph SENT["Batch agent — Sentinel (daily 06:00, Databricks Job)"]
        SCORE["sentinel_core.py<br/>28-day control bands + schema drift diff<br/>vs config/sentinel_registry.json"]
        REP["DRAFT report → analyst<br/><b>human checkpoint</b> — nothing auto-distributed"]
    end

    CHAT -->|question| ASK --> LOOP
    DASH --> DASHAPI --> REG
    LOOP <-->|plan / tool calls| LLML
    LOOP --> T1 & T2 & T3 & T4
    T2 --> GUARD --> DRV
    T4 --> REG --> DRV
    T3 --> KB
    KB -.embeddings.-> L1
    DRV --> D1 & D2
    D1 --> LAKE
    D2 --> LAKE
    BR --> SI --> GO
    SENT -->|read-only SQL| DRV
    SCORE --> REP
    LLML -.narrates findings only.-> REP
```

## The two agents

**1. Interactive agent** (`AgenticFunnelAgent`) — answers natural-language
questions. One hand-written loop: the LLM plans, calls tools, observes
results, fixes its own SQL on error (max 2 retries), then writes the answer.
Every step streams to the browser as an SSE event — the visible "agent trace".
It is *stateless per question* today: no conversation memory (a planned
upgrade adds a rolling window of recent turns).

**2. Sentinel agent** (`sentinel_core` + Job) — runs on a schedule, not on
request. Pure statistics detect anomalies and schema drift; the LLM is only
allowed to *narrate* confirmed findings, never to detect or invent them. Its
output is a DRAFT report behind a human checkpoint. The two agents never
invoke each other; they share the driver, the lakehouse, and the LLM layer.

## Tools (interactive agent)

| Tool | What it does | Safety property |
|---|---|---|
| `get_schema` | Table/column/type inventory + 3 sample rows | read-only, cached |
| `run_sql` | Executes LLM-written SQL | guardrails first: SELECT-only, whitelist, forced LIMIT |
| `search_knowledge` | Retrieves methodology/privacy/insight chunks | citations surfaced to the user |
| `get_metric` | Runs a registry KPI from a gold mart | curated SQL, never LLM-written |

## Models

| Purpose | Model | Where |
|---|---|---|
| Planning, tool calls, answers, TR translation | `gpt-4o-mini` (env-overridable via `AGENT_MODEL`) | OpenAILLM |
| RAG embeddings | `text-embedding-3-small` (disk-cached) | KnowledgeBase |
| Optional alternative provider | Claude (any) | AnthropicLLM |
| Keyless demo / CI | none — deterministic keyword matching | KeywordLLM |

## Memory

- **Static memory (RAG):** `docs/knowledge/*.md` — methodology, privacy rules,
  analytical insights, attribution doctrine. Retrieval-augmented, cited.
- **Schema memory:** `get_schema` cache + `config/sentinel_registry.json`
  (the sentinel's "what normal looks like").
- **Conversation memory:** none yet — each question is independent.
  Planned (quality round): a rolling window of the last N turns so follow-ups
  like "now split that by market" work.
- **No user data is memorised:** the agent stores nothing about viewers;
  the only persisted artefacts are reports it writes.

## Governance spine (what makes this defensible)

One line: **row-level cross-device joins exist only through
`silver.v_attribution_eligible`** — consent gating lives in the schema, the
guardrail whitelist, and the system prompt, not in analyst discipline.
