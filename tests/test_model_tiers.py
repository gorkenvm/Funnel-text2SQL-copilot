"""Tests for model tiers (module M7a): agent.llm.resolve_tier_model /
get_model_tiers / get_llm(tier=...) and the /health tiers reporting.

Hermetic: no network call happens for either provider SDK at construction
time (see tests/test_llm_provider_selection.py's docstring) — these tests
only ever check which model NAME would be used, and that OpenAILLM
instances are cached per resolved model name.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent.llm as llm_module
from agent.llm import (
    DEFAULT_MODEL_TIER,
    MODEL_TIERS,
    get_llm,
    get_model_tiers,
    resolve_tier_config,
    resolve_tier_model,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "AGENT_LLM",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AGENT_MODEL",
        "AGENT_MODEL_FAST",
        "AGENT_MODEL_BALANCED",
        "AGENT_MODEL_MAX",
        "AGENT_REASONING_EFFORT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(llm_module, "_OPENAI_LLM_CACHE", {})
    monkeypatch.setattr(llm_module, "_REASONING_EFFORT_NONE_REQUIRED", set())


class TestResolveTierModel:
    def test_shipped_defaults(self):
        # config/model_tiers.json (module M12): "fast" stays a plain
        # string, "balanced"/"max" are the {"model", "reasoning_effort"}
        # object shape — resolve_tier_model() only ever surfaces the
        # model name either way.
        assert resolve_tier_model("fast") == "gpt-4o-mini"
        assert resolve_tier_model("balanced") == "gpt-5.6-luna"
        assert resolve_tier_model("max") == "gpt-5.6-terra"

    def test_defaults_to_max_tier_when_omitted(self):
        assert resolve_tier_model(None) == resolve_tier_model("max")

    def test_default_tier_constant_is_max(self):
        assert DEFAULT_MODEL_TIER == "max"

    def test_unknown_tier_raises_value_error(self):
        with pytest.raises(ValueError):
            resolve_tier_model("ultra")

    def test_per_tier_env_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_MODEL_FAST", "gpt-4o-mini-custom")
        assert resolve_tier_model("fast") == "gpt-4o-mini-custom"
        # Other tiers stay on their shipped/config default.
        assert resolve_tier_model("balanced") == "gpt-5.6-luna"

    def test_legacy_agent_model_env_wins_over_tier_config(self, monkeypatch):
        monkeypatch.setenv("AGENT_MODEL", "gpt-legacy-pinned")
        for tier in MODEL_TIERS:
            assert resolve_tier_model(tier) == "gpt-legacy-pinned"

    def test_legacy_agent_model_wins_even_over_per_tier_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_MODEL_MAX", "gpt-4o-super")
        monkeypatch.setenv("AGENT_MODEL", "gpt-legacy-pinned")
        assert resolve_tier_model("max") == "gpt-legacy-pinned"


class TestResolveTierConfigM12:
    """Module M12: config/model_tiers.json entries may be a plain string
    OR a {"model", "reasoning_effort"} object; resolve_tier_config()
    surfaces both, resolve_tier_model() keeps returning just the name."""

    def test_plain_string_tier_has_no_reasoning_effort(self):
        cfg = resolve_tier_config("fast")
        assert cfg == {"model": "gpt-4o-mini", "reasoning_effort": None}

    def test_object_shaped_tier_parses_model_and_reasoning_effort(self):
        cfg = resolve_tier_config("balanced")
        assert cfg == {"model": "gpt-5.6-luna", "reasoning_effort": "none"}
        cfg = resolve_tier_config("max")
        assert cfg == {"model": "gpt-5.6-terra", "reasoning_effort": "none"}

    def test_global_reasoning_effort_env_fills_in_unset_tiers(self, monkeypatch):
        # "fast" is a plain string (no reasoning_effort of its own) -- the
        # blanket env var fills it in.
        monkeypatch.setenv("AGENT_REASONING_EFFORT", "low")
        assert resolve_tier_config("fast")["reasoning_effort"] == "low"

    def test_tier_objects_own_reasoning_effort_wins_over_the_global_env(self, monkeypatch):
        # "balanced" already names "none" in the shipped config -- the
        # blanket env var must not override an explicit per-tier value.
        monkeypatch.setenv("AGENT_REASONING_EFFORT", "high")
        assert resolve_tier_config("balanced")["reasoning_effort"] == "none"

    def test_invalid_global_reasoning_effort_is_ignored(self, monkeypatch):
        monkeypatch.setenv("AGENT_REASONING_EFFORT", "extreme")
        assert resolve_tier_config("fast")["reasoning_effort"] is None

    def test_per_tier_model_override_keeps_the_configured_reasoning_effort(self, monkeypatch):
        # AGENT_MODEL_MAX only ever named a plain model string (M7a
        # contract, unchanged) -- overriding the model must not silently
        # drop "max"'s reasoning_effort="none" from the config object.
        monkeypatch.setenv("AGENT_MODEL_MAX", "gpt-5.6-terra-preview")
        cfg = resolve_tier_config("max")
        assert cfg == {"model": "gpt-5.6-terra-preview", "reasoning_effort": "none"}

    def test_legacy_agent_model_env_has_no_reasoning_effort_by_default(self, monkeypatch):
        monkeypatch.setenv("AGENT_MODEL", "gpt-legacy-pinned")
        assert resolve_tier_config("max") == {
            "model": "gpt-legacy-pinned",
            "reasoning_effort": None,
        }

    def test_legacy_agent_model_env_still_honors_the_global_reasoning_effort(self, monkeypatch):
        monkeypatch.setenv("AGENT_MODEL", "gpt-legacy-pinned")
        monkeypatch.setenv("AGENT_REASONING_EFFORT", "medium")
        assert resolve_tier_config("max") == {
            "model": "gpt-legacy-pinned",
            "reasoning_effort": "medium",
        }

    def test_unknown_tier_raises_value_error(self):
        with pytest.raises(ValueError):
            resolve_tier_config("ultra")


class TestGetModelTiers:
    def test_returns_all_three_tiers(self):
        tiers = get_model_tiers()
        assert set(tiers) == set(MODEL_TIERS)

    def test_matches_resolve_tier_model_per_tier(self):
        tiers = get_model_tiers()
        for tier in MODEL_TIERS:
            assert tiers[tier] == resolve_tier_model(tier)


class TestGetLlmTierAware:
    def test_get_llm_no_args_still_works(self):
        # Backward compatibility: get_llm() with zero arguments must keep
        # working exactly as before M7a (defaults to the "max" tier).
        assert get_llm() is not None

    def test_openai_llm_uses_resolved_tier_model(self, monkeypatch):
        from agent.llm import OpenAILLM

        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        llm = get_llm(tier="fast")
        assert isinstance(llm, OpenAILLM)
        assert llm._model == "gpt-4o-mini"
        assert llm._reasoning_effort is None  # "fast" is a plain string entry

    def test_openai_llm_carries_the_tier_objects_reasoning_effort(self, monkeypatch):
        # Module M12: "balanced"/"max" are {"model", "reasoning_effort"}
        # objects in the shipped config — get_llm() must thread that
        # reasoning_effort into the OpenAILLM it builds.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        llm = get_llm(tier="balanced")
        assert llm._model == "gpt-5.6-luna"
        assert llm._reasoning_effort == "none"

    def test_same_tier_reuses_the_cached_instance(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        # "balanced" and "max" resolve to distinct models in the shipped
        # M12 config (gpt-5.6-luna / gpt-5.6-terra) — same tier twice must
        # still share one cached OpenAILLM instance.
        first = get_llm(tier="balanced")
        second = get_llm(tier="balanced")
        assert first is second

    def test_different_models_are_not_shared(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        fast = get_llm(tier="fast")
        balanced = get_llm(tier="balanced")
        assert fast is not balanced

    def test_invalid_tier_falls_back_to_keyword(self, monkeypatch):
        from agent.llm import KeywordLLM

        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        # get_llm() catches resolve_tier_model's ValueError the same way
        # it catches any other construction failure, degrading safely
        # rather than crashing app start-up.
        assert isinstance(get_llm(tier="ultra"), KeywordLLM)


class TestHealthReportsTiers:
    def test_health_reports_tiers_and_default_tier(self):
        from app.main import app

        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["tiers"]) == set(MODEL_TIERS)
        assert body["default_tier"] == "max"
