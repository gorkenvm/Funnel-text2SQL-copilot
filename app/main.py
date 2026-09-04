"""M2: FastAPI web application wrapping the M1 ask-the-funnel agent.

This module intentionally contains no analytics logic of its own — it is a
thin HTTP/JSON layer over ``agent.agent.FunnelAgent`` (module M1). See
``docs/api_contract.md`` for the frontend-facing contract.
"""

from __future__ import annotations

import hmac
import math
import os
import re
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Literal, Optional

# ---------------------------------------------------------------------------
# Make the M1 package importable regardless of CWD (mirrors tests/conftest.py)
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Load a repo-root .env (API keys, AGENT_* settings) when present, so plain
# `uvicorn app.main:app` picks it up like docker compose does. Existing
# environment variables always win; missing python-dotenv is fine.
try:  # pragma: no cover - trivial glue
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=False)
except ImportError:  # pragma: no cover
    pass

import json as _json  # noqa: E402

import pandas as pd  # noqa: E402
from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from agent.agent import FunnelAgent  # noqa: E402
from agent.agentic import AgenticFunnelAgent  # noqa: E402
from agent.db import DuckDBDriver, get_driver  # noqa: E402
from agent import dashboard as dashboard_mod  # noqa: E402
from agent.dashboard import DashboardFilters, FilterValidationError  # noqa: E402
from agent.guardrails import extract_referenced_tables  # noqa: E402
from agent.knowledge import KnowledgeBase  # noqa: E402
from agent.medallion import BRONZE_TABLES, GOLD_TABLES, SILVER_TABLES  # noqa: E402
from agent.llm import (  # noqa: E402
    DEFAULT_MODEL_TIER,
    MODEL_TIERS,
    AnthropicLLM,
    OpenAILLM,
    get_llm,
    get_model_tiers,
)
from agent.memory import ConversationMemory  # noqa: E402

# ---------------------------------------------------------------------------
# Module-level singletons: one driver, one LLM, one agent for the app's life.
# ---------------------------------------------------------------------------
driver = get_driver()
llm = get_llm()  # default ("max") tier — see _llm_for_tier()/tier caches below
agent = FunnelAgent(driver=driver, llm=llm)
knowledge_base = KnowledgeBase()

# M7a: a single shared, in-process conversation memory for the agentic
# loop (see agent.memory.ConversationMemory) — keyed by the client-supplied
# session_id, never by anything server-assigned.
conversation_memory = ConversationMemory()

# M3a: the multi-step agentic loop is only usable with a real (tool-calling)
# LLM provider. With the deterministic KeywordLLM (no API key configured)
# this stays None and /api/ask/stream falls back to the M1/M2 single-shot
# path — see api_ask_stream() below.
agentic_agent: Optional[AgenticFunnelAgent] = (
    AgenticFunnelAgent(driver=driver, llm_chat=llm, knowledge=knowledge_base, memory=conversation_memory)
    if AgenticFunnelAgent.supports_agentic(llm)
    else None
)

# ---------------------------------------------------------------------------
# M7a: model tiers. A request may optionally name a tier ("fast"/
# "balanced"/"max"); only the LLM varies per tier, so drivers/knowledge are
# never rebuilt — each tier gets its own small, lazily-populated LLM/agent
# cache instead. The default tier's objects are exactly the module-level
# `llm`/`agent`/`agentic_agent` singletons above (seeded below), so a
# request that omits `tier` costs nothing extra and behaves exactly as
# before M7a.
# ---------------------------------------------------------------------------
_tier_llm_cache: dict[str, Any] = {DEFAULT_MODEL_TIER: llm}
_tier_agent_cache: dict[str, FunnelAgent] = {DEFAULT_MODEL_TIER: agent}
_tier_agentic_cache: dict[str, Optional[AgenticFunnelAgent]] = {DEFAULT_MODEL_TIER: agentic_agent}


def _tier_key(tier: Optional[str]) -> str:
    return tier or DEFAULT_MODEL_TIER


def _llm_for_tier(tier: Optional[str]) -> Any:
    key = _tier_key(tier)
    if key not in _tier_llm_cache:
        _tier_llm_cache[key] = get_llm(tier)
    return _tier_llm_cache[key]


def _funnel_agent_for_tier(tier: Optional[str]) -> FunnelAgent:
    key = _tier_key(tier)
    if key not in _tier_agent_cache:
        _tier_agent_cache[key] = FunnelAgent(driver=driver, llm=_llm_for_tier(tier))
    return _tier_agent_cache[key]


def _agentic_agent_for_tier(tier: Optional[str]) -> Optional[AgenticFunnelAgent]:
    key = _tier_key(tier)
    if key not in _tier_agentic_cache:
        tier_llm = _llm_for_tier(tier)
        _tier_agentic_cache[key] = (
            AgenticFunnelAgent(
                driver=driver, llm_chat=tier_llm, knowledge=knowledge_base, memory=conversation_memory
            )
            if AgenticFunnelAgent.supports_agentic(tier_llm)
            else None
        )
    return _tier_agentic_cache[key]

_STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Phonak Funnel Copilot API",
    description="Synthetic-data demo API over the ask-the-funnel agent (M1).",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://vmgorken.com",
        "https://www.vmgorken.com",
        "https://funnel.vmgorken.com",
    ],
    allow_origin_regex=r"http://localhost(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# JSON-safety: DataFrame rows can carry pandas Timestamps / numpy scalars /
# NaN, none of which are valid JSON as-is (JS's JSON.parse rejects NaN and
# does not understand Timestamp objects).
# ---------------------------------------------------------------------------
def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        # numpy scalar (int64, float64, bool_, ...) -> native Python type
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


# ---------------------------------------------------------------------------
# Simple in-memory per-IP sliding-window rate limiter for /api/ask.
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window_seconds:
            hits.popleft()
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True


_ask_limiter = RateLimiter(max_requests=20, window_seconds=60.0)
# Separate limiter (own state) for the SSE endpoint, so bursting one
# endpoint in tests or in production never starves the other.
_ask_stream_limiter = RateLimiter(max_requests=20, window_seconds=60.0)

_RATE_LIMIT_MESSAGE = (
    "Too many requests — please wait a moment and try again. / "
    "Çok fazla istek gönderildi — lütfen biraz bekleyip tekrar deneyin."
)


def _client_key(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


# ---------------------------------------------------------------------------
# Module M13: an optional, lightweight demo gate for the public deployment.
# Cost/nuisance protection, NOT real authentication — a shared passphrase in
# one HTTP header, checked in-process with no session/cookie/user concept.
# Entirely off (today's behavior, byte for byte) whenever DEMO_PASSPHRASE is
# unset/empty, which is why it is read fresh from the environment on every
# request rather than cached at import time: it keeps local dev and the test
# suite (which never sets it) completely unaffected, and lets a deployment
# turn the gate on/off by editing .env and restarting, nothing more.
# ---------------------------------------------------------------------------
def _demo_passphrase() -> str:
    return (os.environ.get("DEMO_PASSPHRASE") or "").strip()


def _normalize_demo_key(raw: str) -> str:
    """trim -> collapse internal whitespace runs to one space -> casefold."""
    return " ".join(raw.strip().split()).casefold()


def _demo_key_matches(supplied: str, passphrase: str) -> bool:
    return hmac.compare_digest(
        _normalize_demo_key(supplied).encode("utf-8"),
        _normalize_demo_key(passphrase).encode("utf-8"),
    )


_DEMO_GATE_LOCKED_BODY = {
    "error": "locked",
    "message": "This is a private demo. Enter the passphrase on the page to continue.",
}
_DEMO_GATE_RETRY_MESSAGE = (
    "Too many attempts — please wait a moment and try again. / "
    "Çok fazla deneme yapıldı — lütfen biraz bekleyip tekrar deneyin."
)


class _DemoGateLimiter:
    """Per-IP consecutive-failure counter: 5 wrong keys in a row locks that
    IP out for ``lockout_seconds``. Plain in-memory dicts, no new dependency
    — resets on restart, which is an acceptable tradeoff for a demo gate."""

    def __init__(self, max_failures: int = 5, lockout_seconds: float = 60.0) -> None:
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, int] = defaultdict(int)
        self._locked_until: dict[str, float] = {}

    def is_locked(self, key: str) -> bool:
        until = self._locked_until.get(key)
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        # Lockout expired: clear it so the next failure starts a fresh count.
        del self._locked_until[key]
        self._failures[key] = 0
        return False

    def record_failure(self, key: str) -> None:
        self._failures[key] += 1
        if self._failures[key] >= self.max_failures:
            self._locked_until[key] = time.monotonic() + self.lockout_seconds
            self._failures[key] = 0

    def record_success(self, key: str) -> None:
        self._failures[key] = 0
        self._locked_until.pop(key, None)


_demo_gate_limiter = _DemoGateLimiter()


@app.middleware("http")
async def _demo_gate_middleware(request: Request, call_next):
    """Gates every ``/api/*`` route behind ``X-Demo-Key`` when
    ``DEMO_PASSPHRASE`` is set. Runs as ASGI middleware — BEFORE routing
    picks an endpoint function — specifically so a locked-out or wrong-key
    request never reaches ``FunnelAgent``/``AgenticFunnelAgent``/the driver
    at all (not even to construct a response), and so ``/api/ask/stream``
    never starts its SSE body on a failed check. ``/health`` and the static
    frontend are intentionally NOT under ``/api/`` and stay open either way.
    """
    passphrase = _demo_passphrase()
    if not passphrase or not request.url.path.startswith("/api/"):
        return await call_next(request)

    client_key = _client_key(request)
    if _demo_gate_limiter.is_locked(client_key):
        return JSONResponse(
            status_code=429,
            content={"error": "locked", "message": _DEMO_GATE_RETRY_MESSAGE},
        )

    # EventSource cannot set request headers, so /api/ask/stream alone also
    # accepts the key as a `k` query param (documented in api_contract.md)
    # — every other endpoint only ever honors the header.
    supplied = request.headers.get("X-Demo-Key")
    if not supplied and request.url.path == "/api/ask/stream":
        supplied = request.query_params.get("k")

    if supplied and _demo_key_matches(supplied, passphrase):
        _demo_gate_limiter.record_success(client_key)
        return await call_next(request)

    _demo_gate_limiter.record_failure(client_key)
    return JSONResponse(status_code=401, content=dict(_DEMO_GATE_LOCKED_BODY))


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
#: "fast"/"balanced"/"max" — see agent.llm.MODEL_TIERS. A Literal type here
#: means FastAPI/pydantic itself turns any other value into a 422, with no
#: extra validation code needed (same pattern this file already uses for
#: `lang`).
ModelTier = Literal["fast", "balanced", "max"]

#: "on"/"off" — see agent.memory.ConversationMemory. Default "on": a
#: client that never sends this (nor a session_id) sees no behavioural
#: change at all, since injection additionally requires a session_id with
#: stored history and a referential question.
MemoryMode = Literal["on", "off"]

#: Only a conservative character set may reach agent.memory.ConversationMemory
#: as a session_id (it becomes a dict key and a log-line value) — anything
#: else is stripped rather than rejected, so a slightly-off client value
#: degrades to "no session" instead of a hard 422.
_SESSION_ID_MAX_LEN = 64
_SESSION_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.:-]")


def _sanitize_session_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    cleaned = _SESSION_ID_SAFE_RE.sub("", raw.strip())[:_SESSION_ID_MAX_LEN]
    return cleaned or None


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    lang: Literal["en", "tr"] = "en"
    tier: Optional[ModelTier] = None
    session_id: Optional[str] = Field(default=None, max_length=_SESSION_ID_MAX_LEN)
    memory: MemoryMode = "on"


class DashboardRequest(BaseModel):
    keys: Optional[list[str]] = None
    # Module M11: when set, serves the ~10-KPI filtered dashboard from the
    # gold.web_funnel_daily_cube/gold.journey_daily_cube cubes instead of
    # the legacy top-12 registry — see agent.dashboard.run_dashboard.
    # `keys` is ignored whenever `filters` is present (the two selection
    # mechanisms are for two different registries; mixing them is not a
    # thing this endpoint supports).
    filters: Optional[DashboardFilters] = None


# ---------------------------------------------------------------------------
# Error handling: every error path returns JSON {"error": ...}, never a
# stack trace.
# ---------------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "Invalid request.", "detail": exc.errors()},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error. Please try again later."},
    )


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    driver_name = "duckdb" if isinstance(driver, DuckDBDriver) else "databricks"
    if isinstance(llm, OpenAILLM):
        llm_name = "openai"
    elif isinstance(llm, AnthropicLLM):
        llm_name = "anthropic"
    else:
        llm_name = "keyword"
    max_ts: Optional[str] = None
    try:
        df = driver.query(
            "SELECT MAX(ts) AS max_ts FROM ("
            "SELECT event_timestamp AS ts FROM web_events "
            "UNION ALL SELECT event_timestamp AS ts FROM app_events"
            ") combined"
        )
        if not df.empty:
            max_ts = _json_safe(df.iloc[0]["max_ts"])
    except Exception:  # noqa: BLE001 - health check must never 500
        max_ts = None
    return {
        "status": "ok",
        "driver": driver_name,
        "llm": llm_name,
        "data_max_ts": max_ts,
        # M7a: which concrete model each tier currently resolves to (env
        # overrides included), and which tier a request gets when it
        # doesn't name one.
        "tiers": get_model_tiers(),
        "default_tier": DEFAULT_MODEL_TIER,
        # Module M13: never the passphrase itself — just whether a caller
        # needs one at all, so the frontend knows whether to show the lock
        # screen before it has any key to try.
        "locked": bool(_demo_passphrase()),
    }


@app.post("/api/ask")
def api_ask(payload: AskRequest, request: Request) -> dict:
    if not _ask_limiter.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail=_RATE_LIMIT_MESSAGE)

    tier_agent = _funnel_agent_for_tier(payload.tier)
    result = tier_agent.ask(payload.question, narrate_language=payload.lang)

    # M7a: this endpoint's single-shot FunnelAgent has no tool-use system
    # prompt to inject a PRIOR CONTEXT block into (that is
    # AgenticFunnelAgent/`/api/ask/stream`'s job) — but the turn is still
    # recorded here so a session already has history by the time a client
    # switches to the streaming endpoint for a follow-up.
    session_id = _sanitize_session_id(payload.session_id)
    if session_id:
        conversation_memory.record(
            session_id=session_id,
            question=payload.question,
            tables_used=extract_referenced_tables(result.get("sql")),
            metric_key=result.get("metric_key"),
            one_line_result=result.get("answer") or "",
        )

    response = {
        "question": result.get("question"),
        "mode": result.get("mode"),
        "sql": result.get("sql"),
        "rows": result.get("rows", []),
        "chart": result.get("chart"),
        "answer": result.get("answer"),
        "lang": payload.lang,
    }
    # Module M11: present only for mode == "dashboard" (agent.agent.
    # FunnelAgent._ask_dashboard) — additive, so every pre-M11 response
    # shape is byte-for-byte unchanged for every other mode.
    if "cards" in result:
        response["cards"] = result["cards"]
        response["filter_label"] = result.get("filter_label")
        response["applied_range"] = result.get("applied_range")
    return _json_safe(response)


def _sse_event(payload: dict) -> str:
    """Format one SSE ``data:`` frame carrying a JSON-safe event payload."""
    return f"data: {_json.dumps(_json_safe(payload))}\n\n"


_SSE_DONE_FRAME = "event: done\ndata: {}\n\n"


@app.get("/api/ask/stream")
def api_ask_stream(
    request: Request,
    q: str = Query(..., min_length=1, max_length=2000),
    lang: Literal["en", "tr"] = "en",
    tier: Optional[ModelTier] = None,
    session_id: Optional[str] = Query(default=None, max_length=_SESSION_ID_MAX_LEN),
    memory: MemoryMode = "on",
) -> StreamingResponse:
    """Stream a live agent trace for ``q`` as Server-Sent Events.

    Uses :class:`agent.agentic.AgenticFunnelAgent` (module M3a) when the
    configured LLM supports the tool-use loop; otherwise falls back to a
    single ``plan`` + ``answer`` pair from the M1/M2 :class:`FunnelAgent`
    path, so the stream is always sensible even in keyless demo mode. See
    ``docs/api_contract.md`` for the event schema.

    ``tier``/``session_id``/``memory`` are module M7a additions, all
    optional. ``tier`` selects which model answers this request (see
    ``/health``'s ``tiers``/``default_tier``); ``session_id``/``memory``
    drive :class:`agent.memory.ConversationMemory` — with a real
    tool-use-capable provider, a referential follow-up in the same
    ``session_id`` (``memory="on"``, the default) can trigger the
    "context" event below. The keyless single-shot fallback path has no
    system prompt to inject context into, but still records the turn.
    """
    if not _ask_stream_limiter.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail=_RATE_LIMIT_MESSAGE)

    clean_session_id = _sanitize_session_id(session_id)
    tier_agentic_agent = _agentic_agent_for_tier(tier)

    def event_stream():
        try:
            if tier_agentic_agent is not None:
                for event in tier_agentic_agent.run(
                    q,
                    lang=lang,
                    session_id=clean_session_id,
                    memory_enabled=(memory == "on"),
                ):
                    yield _sse_event(event)
            else:
                yield _sse_event(
                    {
                        "type": "plan",
                        "text": f"Answering with the registered-KPI planner: {q!r}",
                    }
                )
                tier_agent = _funnel_agent_for_tier(tier)
                result = tier_agent.ask(q, narrate_language=lang)
                if clean_session_id:
                    conversation_memory.record(
                        session_id=clean_session_id,
                        question=q,
                        tables_used=extract_referenced_tables(result.get("sql")),
                        metric_key=result.get("metric_key"),
                        one_line_result=result.get("answer") or "",
                    )
                if result.get("mode") == "dashboard":
                    # Module M11: the keyless deterministic dashboard path
                    # (agent.agent.FunnelAgent._ask_dashboard via
                    # agent.llm.KeywordLLM's dashboard intent) — same
                    # "dashboard" event shape the agentic loop emits.
                    yield _sse_event(
                        {
                            "type": "dashboard",
                            "cards": result.get("cards", []),
                            "filter_label": result.get("filter_label"),
                            "applied_range": result.get("applied_range"),
                        }
                    )
                yield _sse_event(
                    {
                        "type": "answer",
                        "answer": result.get("answer"),
                        "sql": result.get("sql"),
                        "rows": result.get("rows", []),
                        "chart": result.get("chart"),
                        "citations": [],
                    }
                )
        except Exception:  # noqa: BLE001 - never break the stream with a stack trace
            yield _sse_event(
                {"type": "error", "message": "Internal error while answering."}
            )
            yield _sse_event(
                {
                    "type": "answer",
                    "answer": "Something went wrong answering that. Please try again.",
                    "sql": None,
                    "rows": [],
                    "chart": None,
                    "citations": [],
                }
            )
        yield _SSE_DONE_FRAME

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/metrics")
def api_metrics() -> list[dict]:
    out = []
    for metric in agent.registry.values():
        out.append(
            {
                "key": metric["key"],
                "title": metric["title"],
                "description": metric["description"].strip(),
                "consent_note": (metric.get("consent_note") or "").strip(),
                "chart": metric.get("chart"),
            }
        )
    return _json_safe(out)


@app.post("/api/dashboard")
def api_dashboard(payload: DashboardRequest) -> list[dict]:
    if payload.filters is not None:
        # Module M11: filtered path — the ~10-KPI dimensional-gold-cube
        # registry, never the legacy top-12 `keys` selection.
        try:
            dashboard_mod.validate_filters(payload.filters, driver)
        except FilterValidationError as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid filter '{exc.field}': {exc}"
            )
        result = dashboard_mod.run_dashboard(driver, payload.filters)
        return _json_safe(result["cards"])

    keys = payload.keys or list(agent.registry.keys())
    unknown = [k for k in keys if k not in agent.registry]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown metric key(s): {', '.join(unknown)}",
        )

    cards = []
    for key in keys:
        result = agent.run_metric(key)
        cards.append(
            {
                "key": key,
                "title": result.get("title"),
                "consent_note": result.get("consent_note", ""),
                "chart": result.get("chart"),
                "rows": result.get("rows", []),
                "answer": result.get("answer"),
                # M11 addendum 2: agent.run_metric() already runs this
                # through the shared sqlfmt choke point via _payload().
                "sql": result.get("sql"),
            }
        )
    return _json_safe(cards)


#: Ordered (layer_name, bare_table_names) pairs backing GET /api/catalog —
#: reuses the same medallion inventory tuples that agent.guardrails' table
#: whitelist is built from, so the catalog can never list an object the
#: agent itself is not allowed to query.
_CATALOG_LAYERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bronze", BRONZE_TABLES),
    ("silver", SILVER_TABLES),
    ("gold", GOLD_TABLES),
)


def _catalog_comments() -> dict[str, str]:
    """Best-effort ``{"layer.object": comment}`` lookup for the M8 data catalog.

    Two whole-catalog queries (tables, then views) — never one per object,
    per the M8 spec's "skip if it needs extra queries per object" rule.
    DuckDB exposes COMMENT ON text via its ``duckdb_tables()``/
    ``duckdb_views()`` introspection functions; a production Databricks
    driver has no such functions, so any failure here is swallowed and the
    catalog simply ships without comments rather than 500ing.
    """
    comments: dict[str, str] = {}
    queries = [
        "SELECT schema_name, table_name AS obj_name, comment FROM duckdb_tables() "
        "WHERE schema_name IN ('bronze', 'silver', 'gold')",
        "SELECT schema_name, view_name AS obj_name, comment FROM duckdb_views() "
        "WHERE schema_name IN ('bronze', 'silver', 'gold')",
    ]
    for sql in queries:
        try:
            df = driver.query(sql)
        except Exception:  # noqa: BLE001 - comments are a nice-to-have, never fatal
            continue
        for _, row in df.iterrows():
            comment = row.get("comment")
            if comment:
                comments[f"{row['schema_name']}.{row['obj_name']}"] = str(comment)
    return comments


@app.get("/api/catalog")
def api_catalog() -> dict:
    """The medallion table/view inventory for the frontend's data catalog panel.

    See ``docs/api_contract.md`` for the response contract. Row counts are
    deliberately omitted (module M8 spec: skip anything needing a query per
    object) — only the qualified name and, when cheaply available, its
    ``COMMENT ON`` text.
    """
    comments = _catalog_comments()

    def _layer_entries(layer: str, tables: tuple[str, ...]) -> list[dict]:
        entries = []
        for name in tables:
            qualified = f"{layer}.{name}"
            entry: dict[str, Any] = {"name": qualified}
            comment = comments.get(qualified)
            if comment:
                entry["comment"] = comment
            entries.append(entry)
        return entries

    return _json_safe(
        {"layers": {layer: _layer_entries(layer, tables) for layer, tables in _CATALOG_LAYERS}}
    )


# ---------------------------------------------------------------------------
# Static frontend (app/static/index.html served at "/")
# ---------------------------------------------------------------------------
if _STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
