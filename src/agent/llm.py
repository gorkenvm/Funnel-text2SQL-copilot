"""LLM planning and agentic chat layer.

Two distinct protocols are exposed on an :class:`LLMClient`:

* ``plan(question, schema_doc, metric_keys) -> dict`` — the single-shot
  planner used by :class:`agent.agent.FunnelAgent` (module M1/M2). It
  turns a natural-language question into a plan:
  ``{"mode": "metric" | "sql" | "clarify", "metric_key": ..., "sql": ...,
  "narrative_hint": ...}``.
* ``chat_step(messages, tools) -> dict`` — the provider-agnostic
  tool-use step used by :class:`agent.agentic.AgenticFunnelAgent` (module
  M3a). Given a canonical chat history and a list of tool definitions, it
  returns one assistant turn: ``{"content": str | None, "tool_calls":
  [{"id": str, "name": str, "arguments": dict}, ...]}``. An empty
  ``tool_calls`` list means the model is done and ``content`` is the
  final answer.

Canonical chat message shapes (used by every ``chat_step`` implementation
and by :mod:`agent.agentic`, translated to each provider's own format
internally):

* ``{"role": "system", "content": str}``
* ``{"role": "user", "content": str}``
* ``{"role": "assistant", "content": str | None, "tool_calls": [...]}``
* ``{"role": "tool", "tool_call_id": str, "name": str, "content": str}``
  (``content`` is a JSON string — the tool's result or error)

Implementations:

* :class:`OpenAILLM` — uses the ``openai`` package (lazy import) and its
  native function-calling for ``chat_step``. Selected automatically when
  ``OPENAI_API_KEY`` is set.
* :class:`AnthropicLLM` — uses the ``anthropic`` package (lazy import)
  and its tool-use content blocks for ``chat_step``. Selected
  automatically when ``ANTHROPIC_API_KEY`` is set (and no OpenAI key).
* :class:`KeywordLLM` — deterministic keyword matcher against the metric
  registry. Implements ``plan`` only — it deliberately does NOT implement
  ``chat_step``, so the agentic loop requires a real provider (or the
  :class:`agent.testing.ScriptedLLM` test double). Used in tests and
  whenever no API key is available.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional, Protocol, Sequence

logger = logging.getLogger(__name__)

ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-5"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

#: Backwards-compatible alias (module used to only support Anthropic).
DEFAULT_MODEL = ANTHROPIC_DEFAULT_MODEL

# ---------------------------------------------------------------------------
# Model tiers (module M7a): "fast" / "balanced" / "max" resolve to concrete
# OpenAI model names via config/model_tiers.json, each overridable by an
# AGENT_MODEL_<TIER> env var — see get_llm()/resolve_tier_model() below and
# the README / docs/deploy_guide.md for the deployment-facing story (in
# short: edit config/model_tiers.json's "max" entry, or set
# AGENT_MODEL_MAX, to point at the strongest model your OpenAI account has
# access to).
# ---------------------------------------------------------------------------
MODEL_TIERS: tuple[str, ...] = ("fast", "balanced", "max")

#: User decision (quality over cost): when a caller doesn't specify a tier,
#: resolve to the strongest configured model.
DEFAULT_MODEL_TIER = "max"

#: Shipped fallback mapping, used whenever config/model_tiers.json is
#: missing, unreadable, or missing an entry for a given tier — so the
#: agent never fails to resolve a model just because the config file was
#: deleted or trimmed.
_SHIPPED_MODEL_TIERS: dict[str, str] = {
    "fast": "gpt-4o-mini",
    "balanced": "gpt-4o",
    "max": "gpt-4o",
}

_MODEL_TIERS_PATH = Path(__file__).resolve().parents[2] / "config" / "model_tiers.json"

_TIER_ENV_VARS: dict[str, str] = {
    "fast": "AGENT_MODEL_FAST",
    "balanced": "AGENT_MODEL_BALANCED",
    "max": "AGENT_MODEL_MAX",
}

#: Module M12: a config/model_tiers.json entry may be either a plain model
#: name (unchanged since M7a) or an object naming a per-model
#: ``reasoning_effort`` to send on every OpenAI chat.completions call for
#: that tier — needed for newer OpenAI reasoning models that 400 on
#: /v1/chat/completions when function tools are combined with a
#: reasoning_effort other than "none" (see OpenAILLM._create_chat_completion
#: for the self-healing fallback if this is left unset or wrong).
_REASONING_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high")


def _parse_tier_entry(value: object) -> Optional[dict]:
    """Normalize one ``config/model_tiers.json`` value into
    ``{"model": str, "reasoning_effort": str | None}``, or ``None`` if
    ``value`` is neither shape (a plain non-empty string, or an object with
    a non-empty ``"model"`` string and an optional ``"reasoning_effort"``
    one of :data:`_REASONING_EFFORTS`) — callers keep the previous tier's
    value in that case, exactly like the old "not a non-empty string" skip.
    """
    if isinstance(value, str):
        return {"model": value.strip(), "reasoning_effort": None} if value.strip() else None
    if isinstance(value, dict):
        model = value.get("model")
        if not (isinstance(model, str) and model.strip()):
            return None
        effort = value.get("reasoning_effort")
        if isinstance(effort, str) and effort.strip().lower() in _REASONING_EFFORTS:
            effort = effort.strip().lower()
        else:
            effort = None
        return {"model": model.strip(), "reasoning_effort": effort}
    return None


def _load_model_tiers_full() -> dict[str, dict]:
    """Every tier resolved to ``{"model": str, "reasoning_effort": str | None}``.

    Layering, lowest to highest precedence:

    1. The shipped defaults (plain model names, no reasoning_effort).
    2. ``config/model_tiers.json`` — each entry a plain string or a
       ``{"model", "reasoning_effort"}`` object (:func:`_parse_tier_entry`).
    3. ``AGENT_REASONING_EFFORT`` (module M12) fills in ``reasoning_effort``
       for any tier that didn't already get one from the config object —
       an explicit per-tier value in the JSON always wins over this
       blanket env var.
    4. ``AGENT_MODEL_<TIER>`` overrides that tier's **model** only (its
       ``reasoning_effort`` from steps 2-3 is kept) — unchanged M7a
       contract, just no longer collapsing the object shape.

    Never raises: a missing/unreadable/malformed config file just means
    the shipped defaults are used as-is, exactly like before M12.
    """
    tiers: dict[str, dict] = {
        tier: {"model": model, "reasoning_effort": None}
        for tier, model in _SHIPPED_MODEL_TIERS.items()
    }
    try:
        with open(_MODEL_TIERS_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            for key, value in data.items():
                parsed = _parse_tier_entry(value)
                if parsed is not None:
                    tiers[key] = parsed
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass  # shipped defaults stand

    global_effort = os.environ.get("AGENT_REASONING_EFFORT")
    if global_effort and global_effort.strip().lower() in _REASONING_EFFORTS:
        global_effort = global_effort.strip().lower()
        for cfg in tiers.values():
            if cfg["reasoning_effort"] is None:
                cfg["reasoning_effort"] = global_effort

    for tier, env_var in _TIER_ENV_VARS.items():
        override = os.environ.get(env_var)
        if override and override.strip():
            tiers[tier] = {
                "model": override.strip(),
                "reasoning_effort": tiers[tier]["reasoning_effort"],
            }
    return tiers


def _load_model_tiers_config() -> dict[str, str]:
    """Backward-compatible view of :func:`_load_model_tiers_full`: model
    names only, no ``reasoning_effort`` — kept for any external caller
    that only ever needed the model name (:func:`resolve_tier_model`,
    :func:`get_model_tiers`)."""
    return {tier: cfg["model"] for tier, cfg in _load_model_tiers_full().items()}


def resolve_tier_config(tier: Optional[str] = None) -> dict:
    """Resolve a model tier to ``{"model": str, "reasoning_effort": str | None}``.

    Same resolution order as :func:`resolve_tier_model` (the legacy
    ``AGENT_MODEL`` env var always wins on the model name regardless of
    tier), plus ``reasoning_effort`` from ``config/model_tiers.json``'s
    object shape or the blanket ``AGENT_REASONING_EFFORT`` env var (module
    M12) — see :func:`_load_model_tiers_full`. Raises ``ValueError`` for an
    unknown ``tier``, exactly like :func:`resolve_tier_model`.
    """
    legacy = os.environ.get("AGENT_MODEL")
    if legacy and legacy.strip():
        effort = os.environ.get("AGENT_REASONING_EFFORT")
        effort = effort.strip().lower() if effort and effort.strip().lower() in _REASONING_EFFORTS else None
        return {"model": legacy.strip(), "reasoning_effort": effort}

    key = (tier or DEFAULT_MODEL_TIER).strip().lower()
    if key not in MODEL_TIERS:
        raise ValueError(
            f"Unknown model tier {tier!r}. Known tiers: {', '.join(MODEL_TIERS)}."
        )
    return _load_model_tiers_full()[key]


def resolve_tier_model(tier: Optional[str] = None) -> str:
    """Resolve a model tier ("fast"/"balanced"/"max") to a concrete model name.

    Resolution order:

    1. The legacy ``AGENT_MODEL`` env var, if set, **always** wins,
       regardless of ``tier`` — this keeps every pre-M7a deployment that
       already pins a model via ``AGENT_MODEL`` working unchanged.
    2. Otherwise, ``config/model_tiers.json`` (merged over the shipped
       defaults), with each tier individually overridable by
       ``AGENT_MODEL_FAST`` / ``AGENT_MODEL_BALANCED`` / ``AGENT_MODEL_MAX``.

    ``tier=None`` resolves to :data:`DEFAULT_MODEL_TIER` ("max").

    Raises ``ValueError`` for a ``tier`` that is not one of
    :data:`MODEL_TIERS` — callers exposing this over an API should turn
    that into a 422, not a 500. Model-name-only view of
    :func:`resolve_tier_config` — see that function for the M12
    ``reasoning_effort`` companion value.
    """
    return resolve_tier_config(tier)["model"]


def get_model_tiers() -> dict[str, str]:
    """The resolved model name for every known tier — for ``/health``.

    Mirrors :func:`resolve_tier_model`'s resolution order per tier (so if
    the legacy ``AGENT_MODEL`` env var is set, every tier alike reports
    that same overriding model — it really does win regardless of tier).
    """
    return {tier: resolve_tier_model(tier) for tier in MODEL_TIERS}


#: OpenAILLM instances are cached per resolved model name (module M7a) so
#: that answering two requests at the same tier — or two different tiers
#: that happen to resolve to the same underlying model — reuses one
#: client/agent rather than reconstructing the OpenAI SDK client each time.
_OPENAI_LLM_CACHE: dict[str, "OpenAILLM"] = {}


def _cached_openai_llm(
    model: str, api_key: Optional[str] = None, reasoning_effort: Optional[str] = None
) -> "OpenAILLM":
    cached = _OPENAI_LLM_CACHE.get(model)
    if cached is not None:
        return cached
    llm = OpenAILLM(model=model, api_key=api_key, reasoning_effort=reasoning_effort)
    _OPENAI_LLM_CACHE[model] = llm
    return llm

#: Keyword hints per metric key, used by :class:`KeywordLLM` (substring,
#: case-insensitive). Order matters: on a score tie the earlier entry wins.
_METRIC_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("funnel_overview", ("funnel", "overview", "per stage", "each stage")),
    ("step_conversion_rates", ("drop-off", "drop off", "dropoff", "biggest drop", "conversion", "step")),
    ("completion_rate_by_channel", ("complet", "channel")),
    ("completion_rate_by_device", ("complet", "device", "desktop", "mobile", "tablet")),
    ("downloads_by_channel", ("download", "install")),
    ("pairing_rate_by_channel", ("pairing", "paired", "pair", "channel")),
    ("pairing_rate_by_platform_market", ("pairing", "paired", "pair", "ios", "android", "platform")),
    ("d30_retention_by_channel", ("d30", "retention", "retained", "still active", "day 30")),
    ("weekly_test_starts_trend", ("weekly", "trend", "over time", "per week", "starts")),
    ("linkable_share_by_market", ("link", "linkable", "across web and app", "bridge", "match")),
    (
        "attribution_first_vs_last",
        (
            "first touch",
            "first-touch",
            "last touch",
            "last-touch",
            "first vs last",
            "attribution model",
            "different stories",
        ),
    ),
    (
        "completion_by_channel_device",
        (
            "complet",
            "channel",
            "device",
            "channel and device",
            "channel x device",
            "channel × device",
            "channel by device",
        ),
    ),
]


class LLMClient(Protocol):
    """Anything that can turn a question into an execution plan."""

    def plan(
        self, question: str, schema_doc: str, metric_keys: Sequence[str]
    ) -> dict:
        """Return a plan dict with keys mode / metric_key? / sql? / narrative_hint?."""
        ...


class KeywordLLM:
    """Deterministic fallback planner: match question keywords to a metric.

    Never emits free-form SQL — it either picks a registry metric or asks
    for clarification, which makes it safe and fully reproducible for
    tests and offline demos. Does not implement ``chat_step``: the
    agentic loop (module M3a) is unavailable with this planner.
    """

    def plan(
        self, question: str, schema_doc: str, metric_keys: Sequence[str]
    ) -> dict:
        # M11: dashboard/KPI-board intent is checked BEFORE the single-metric
        # keyword match below (a dashboard question would otherwise often
        # also match a metric keyword, e.g. "overview") and needs no driver
        # access here — agent.dashboard.extract_filters_from_text is pure
        # regex; agent.agent.FunnelAgent._ask_dashboard resolves any
        # relative-range phrase against the driver's actual data horizon.
        from agent.dashboard import extract_filters_from_text, is_dashboard_intent

        if is_dashboard_intent(question):
            return {"mode": "dashboard", "filters": extract_filters_from_text(question)}

        q = question.lower()
        best_key: Optional[str] = None
        best_score = 0
        for key, keywords in _METRIC_KEYWORDS:
            if key not in metric_keys:
                continue
            score = sum(1 for kw in keywords if kw in q)
            if score > best_score:  # strict '>' keeps registry order on ties
                best_key, best_score = key, score
        if best_key is None:
            return {
                "mode": "clarify",
                "narrative_hint": (
                    "I could not map the question to a known KPI. "
                    "Try asking about the funnel, drop-off, completion, "
                    "downloads, pairing, retention, weekly trends or "
                    "linkable users."
                ),
            }
        return {"mode": "metric", "metric_key": best_key}


# ---------------------------------------------------------------------------
# Shared helpers for the real (API-backed) planners.
# ---------------------------------------------------------------------------
def _planner_system_prompt(schema_doc: str, metric_keys: Sequence[str]) -> str:
    """System prompt for the single-shot ``plan()`` call, shared by every
    real LLM provider so their behaviour (and prompt-engineering effort)
    stays in one place."""
    return (
        "You are the planning brain of an analytics agent for a "
        "hearing-test funnel (web -> app).\n\n"
        "DATA SCHEMA AND KPI REGISTRY:\n"
        f"{schema_doc}\n\n"
        f"Registered metric keys: {', '.join(metric_keys)}\n\n"
        "GUARDRAILS for any SQL you write:\n"
        "- one single SELECT/WITH statement, read-only, no semicolons;\n"
        "- only the tables web_events, app_events, id_bridge;\n"
        "- cross-device web->app joins MUST go through id_bridge "
        "(web_pseudo_id <-> app_device_id) — never join web and app "
        "events directly;\n"
        "- DuckDB/Databricks-compatible ANSI SQL, aggregate results "
        "only (no raw event dumps).\n\n"
        "Answer with ONE strict JSON object and nothing else:\n"
        '{"mode": "metric"|"sql"|"clarify", "metric_key": "<key>", '
        '"sql": "<query>", "narrative_hint": "<one sentence>"}\n'
        "Rules: prefer mode=metric with a metric_key from the registry "
        "when a registered KPI answers the question; use mode=sql only "
        "for questions no registered KPI covers; use mode=clarify when "
        "the question is unanswerable with this data."
    )


def _parse_plan_text(text: str) -> dict:
    """Parse a model reply into a plan dict (tolerating code fences)."""
    candidate = text
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    brace = re.search(r"\{.*\}", candidate, re.DOTALL)
    if brace:
        candidate = brace.group(0)
    try:
        plan = json.loads(candidate)
    except json.JSONDecodeError:
        return {
            "mode": "clarify",
            "narrative_hint": "The planner returned an unreadable plan; "
            "please rephrase the question.",
        }
    if plan.get("mode") not in {"metric", "sql", "clarify"}:
        plan["mode"] = "clarify"
    return plan


def _translate_system_prompt(target_language: str) -> str:
    return (
        "Translate the following analytics answer into "
        f"{target_language}. Preserve every number, percentage and "
        "metric/column name exactly as given. Reply with the "
        "translation only, no preamble or quotes."
    )


class AnthropicLLM:
    """Planner + agentic chat backed by the Anthropic API (lazy import).

    ``plan()`` embeds the schema documentation, the metric registry and
    the guardrail rules in the system prompt and asks the model to answer
    with a single strict-JSON object. ``chat_step()`` implements the
    provider-agnostic tool-use protocol using Anthropic's tool-use content
    blocks, for the agentic loop in :mod:`agent.agentic`.
    """

    def __init__(
        self, model: Optional[str] = None, api_key: Optional[str] = None
    ) -> None:
        try:
            import anthropic  # lazy import by design
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "AnthropicLLM requires the optional dependency 'anthropic' "
                "(pip install anthropic)."
            ) from exc
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )
        self._model = model or os.environ.get("AGENT_MODEL", ANTHROPIC_DEFAULT_MODEL)

    def plan(
        self, question: str, schema_doc: str, metric_keys: Sequence[str]
    ) -> dict:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1200,
            system=_planner_system_prompt(schema_doc, metric_keys),
            messages=[{"role": "user", "content": question}],
        )
        text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip()
        return _parse_plan_text(text)

    def translate(self, text: str, target_language: str) -> str:
        """Best-effort translation of a short narrative answer.

        Used by :meth:`agent.agent.FunnelAgent.ask` when a caller requests a
        non-English narrative (e.g. Turkish for the M2 web UI). Numbers and
        metric names are asked to be preserved verbatim.
        """
        response = self._client.messages.create(
            model=self._model,
            max_tokens=500,
            system=_translate_system_prompt(target_language),
            messages=[{"role": "user", "content": text}],
        )
        return "".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip()

    def chat_step(self, messages: list[dict], tools: list[dict]) -> dict:
        """One tool-use turn for the agentic loop (Anthropic tool blocks)."""
        system_text, anthropic_messages = _to_anthropic_messages(messages)
        anthropic_tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tools
        ]
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1500,
            system=system_text or "",
            messages=anthropic_messages,
            tools=anthropic_tools,
        )
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
            elif getattr(block, "type", None) == "tool_use":
                tool_calls.append(
                    {"id": block.id, "name": block.name, "arguments": dict(block.input)}
                )
        return {
            "content": "\n".join(text_parts) if text_parts else None,
            "tool_calls": tool_calls,
        }


def _to_anthropic_messages(messages: list[dict]) -> tuple[Optional[str], list[dict]]:
    """Translate canonical chat messages into Anthropic's message shape."""
    system_text: Optional[str] = None
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            system_text = m.get("content")
        elif role == "user":
            out.append({"role": "user", "content": m.get("content") or ""})
        elif role == "assistant":
            content: list[dict] = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["arguments"],
                    }
                )
            out.append({"role": "assistant", "content": content or ""})
        elif role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m["tool_call_id"],
                            "content": m["content"],
                        }
                    ],
                }
            )
    return system_text, out


#: Module M12: once a model name is found to reject `reasoning_effort` on
#: /v1/chat/completions (the exact 400 OpenAILLM self-heals from — see
#: OpenAILLM._create_chat_completion), every OpenAILLM instance for that
#: model name is remembered here for the rest of the process, so later
#: calls skip straight to reasoning_effort="none" instead of repeating the
#: failing attempt. Deliberately NOT a hardcoded model-name list — this is
#: populated only from an actual 400 response, so it works for any future
#: model name without a code change.
_REASONING_EFFORT_NONE_REQUIRED: set[str] = set()


def _looks_like_reasoning_effort_400(exc: Exception) -> bool:
    """Loose match for OpenAI's "Function tools with reasoning_effort are
    not supported for <model> in /v1/chat/completions ... set
    reasoning_effort to 'none'" 400 — matched on the error TEXT, never a
    model name, since new reasoning models ship faster than this code can
    be updated with their names. Deliberately loose (mentions of both
    "reasoning_effort" and something naming the endpoint/tool-calling
    conflict) rather than an exact string match, so minor wording changes
    in the provider's message don't silently stop the self-heal."""
    message = str(exc).lower()
    if "reasoning_effort" not in message:
        return False
    return any(
        marker in message
        for marker in ("function tool", "/v1/responses", "chat/completions", "chat completions")
    )


class OpenAILLM:
    """Planner + agentic chat backed by the OpenAI API (lazy import).

    ``plan()`` mirrors :class:`AnthropicLLM`'s single-shot planning
    prompt. ``chat_step()`` implements the provider-agnostic tool-use
    protocol using OpenAI's native function-calling, for the agentic loop
    in :mod:`agent.agentic`. Reads ``OPENAI_API_KEY`` and the model name
    from ``AGENT_MODEL`` (default ``"gpt-4o-mini"``).

    ``reasoning_effort`` (module M12, optional) is sent on every
    chat.completions call for this model — set it via
    ``config/model_tiers.json``'s object shape or the blanket
    ``AGENT_REASONING_EFFORT`` env var for a newer OpenAI reasoning model
    that needs one. If it is left unset (or set wrong) and the model
    itself insists on ``reasoning_effort="none"`` when function tools are
    in play, :meth:`_create_chat_completion` self-heals: it catches that
    specific 400 once, retries with ``reasoning_effort="none"``, and
    remembers the fix for this model name for the rest of the process
    (see :data:`_REASONING_EFFORT_NONE_REQUIRED`).
    """

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        try:
            import openai  # lazy import by design
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "OpenAILLM requires the optional dependency 'openai' "
                "(pip install openai)."
            ) from exc
        # Explicit per-request timeout + a single retry: the SDK's defaults
        # (10-minute timeout, 2 silent backoff retries) can quietly consume
        # the agent loop's whole wall-clock budget on one flaky request.
        self._client = openai.OpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
            timeout=float(os.environ.get("AGENT_LLM_TIMEOUT_SECONDS", "45")),
            max_retries=1,
        )
        self._model = model or os.environ.get("AGENT_MODEL", OPENAI_DEFAULT_MODEL)
        self._reasoning_effort = reasoning_effort

    def _create_chat_completion(self, **kwargs):
        """``self._client.chat.completions.create(**kwargs)`` with the
        configured ``reasoning_effort`` applied and module M12's 400
        self-healing (see :data:`_REASONING_EFFORT_NONE_REQUIRED` and
        :func:`_looks_like_reasoning_effort_400`): every caller
        (``plan``/``translate``/``chat_step``) routes through here so all
        three benefit identically."""
        effort = "none" if self._model in _REASONING_EFFORT_NONE_REQUIRED else self._reasoning_effort
        call_kwargs = dict(kwargs)
        if effort:
            call_kwargs["reasoning_effort"] = effort
        try:
            return self._client.chat.completions.create(**call_kwargs)
        except Exception as exc:  # noqa: BLE001 - inspected below, re-raised if not this case
            if effort == "none" or not _looks_like_reasoning_effort_400(exc):
                raise
            _REASONING_EFFORT_NONE_REQUIRED.add(self._model)
            logger.warning(
                "OpenAI model %r requires reasoning_effort=none on "
                "chat.completions -- applied automatically",
                self._model,
            )
            retry_kwargs = dict(kwargs)
            retry_kwargs["reasoning_effort"] = "none"
            return self._client.chat.completions.create(**retry_kwargs)

    def plan(
        self, question: str, schema_doc: str, metric_keys: Sequence[str]
    ) -> dict:
        response = self._create_chat_completion(
            model=self._model,
            messages=[
                {"role": "system", "content": _planner_system_prompt(schema_doc, metric_keys)},
                {"role": "user", "content": question},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        return _parse_plan_text(text)

    def translate(self, text: str, target_language: str) -> str:
        """Best-effort translation of a short narrative answer (see AnthropicLLM.translate)."""
        response = self._create_chat_completion(
            model=self._model,
            messages=[
                {"role": "system", "content": _translate_system_prompt(target_language)},
                {"role": "user", "content": text},
            ],
        )
        return (response.choices[0].message.content or "").strip()

    def chat_step(self, messages: list[dict], tools: list[dict]) -> dict:
        """One tool-use turn for the agentic loop (OpenAI function-calling)."""
        openai_messages = _to_openai_messages(messages)
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]
        response = self._create_chat_completion(
            model=self._model,
            messages=openai_messages,
            tools=openai_tools,
            tool_choice="auto",
        )
        choice = response.choices[0].message
        tool_calls: list[dict] = []
        for tc in getattr(choice, "tool_calls", None) or []:
            try:
                arguments = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                {"id": tc.id, "name": tc.function.name, "arguments": arguments}
            )
        return {"content": choice.content, "tool_calls": tool_calls}


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    """Translate canonical chat messages into OpenAI's message shape."""
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "assistant" and m.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("content"),
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"]),
                            },
                        }
                        for tc in m["tool_calls"]
                    ],
                }
            )
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m["tool_call_id"],
                    "content": m["content"],
                }
            )
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def get_llm(tier: Optional[str] = None) -> LLMClient:
    """Factory selecting the LLM provider (module M7a: now tier-aware).

    Resolution order:

    1. ``AGENT_LLM`` env var when set to ``"openai"``, ``"anthropic"`` or
       ``"keyword"`` — explicit choice wins even if the corresponding
       package/key is missing (falls back to :class:`KeywordLLM` if the
       optional package cannot be imported).
    2. Otherwise, auto-detect: ``OPENAI_API_KEY`` set -> :class:`OpenAILLM`;
       else ``ANTHROPIC_API_KEY`` set -> :class:`AnthropicLLM`; else
       :class:`KeywordLLM`.

    ``tier`` ("fast"/"balanced"/"max", default :data:`DEFAULT_MODEL_TIER`
    when omitted) only affects which model (and, module M12, which
    ``reasoning_effort``) an :class:`OpenAILLM` is built with, via
    :func:`resolve_tier_config` — see that function for the ``AGENT_MODEL``
    legacy-override / ``config/model_tiers.json`` resolution order. It has
    no effect on :class:`AnthropicLLM` or
    :class:`KeywordLLM` (there is no tiered model list for either — an
    explicit ``AGENT_MODEL`` still overrides AnthropicLLM's model exactly
    as before M7a). :class:`OpenAILLM` instances are cached per resolved
    model name (see :func:`_cached_openai_llm`), so calling this
    repeatedly for the same tier is cheap.
    """
    # Broad `except Exception` below is intentional: a provider client can
    # fail to construct for more reasons than a missing package (e.g. the
    # openai SDK itself raises if no credentials are configured at all) —
    # any such failure should degrade to the deterministic planner rather
    # than crash app start-up.
    choice = (os.environ.get("AGENT_LLM") or "").strip().lower()
    if choice == "openai":
        try:
            cfg = resolve_tier_config(tier)
            return _cached_openai_llm(cfg["model"], reasoning_effort=cfg["reasoning_effort"])
        except Exception:  # noqa: BLE001
            return KeywordLLM()
    if choice == "anthropic":
        try:
            return AnthropicLLM()
        except Exception:  # noqa: BLE001
            return KeywordLLM()
    if choice == "keyword":
        return KeywordLLM()

    if os.environ.get("OPENAI_API_KEY"):
        try:
            cfg = resolve_tier_config(tier)
            return _cached_openai_llm(cfg["model"], reasoning_effort=cfg["reasoning_effort"])
        except Exception:  # noqa: BLE001
            pass  # fall through to the next provider
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicLLM()
        except Exception:  # noqa: BLE001
            pass  # fall back to the deterministic planner
    return KeywordLLM()
