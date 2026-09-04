"""Provider-selection tests for agent.llm.get_llm().

These only ever construct client objects (no network call happens at
construction time for either the openai or anthropic SDK) — nothing here
calls .plan()/.chat_step(), so no request ever reaches a real API.
"""

from __future__ import annotations

import pytest

import agent.llm as llm_module
from agent.llm import KeywordLLM, OpenAILLM, get_llm


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
    ):
        monkeypatch.delenv(var, raising=False)
    # Module M7a: get_llm() caches OpenAILLM instances per resolved model
    # name across calls (by design — see agent.llm._cached_openai_llm).
    # That cache is process-global, so without resetting it here a
    # successful construction in one test (with a fake key) would leak
    # into a later test that expects construction to fail (no key at
    # all) — clear it before every test in this module for isolation.
    monkeypatch.setattr(llm_module, "_OPENAI_LLM_CACHE", {})


def test_defaults_to_keyword_with_no_keys():
    assert isinstance(get_llm(), KeywordLLM)


def test_openai_key_selects_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    assert isinstance(get_llm(), OpenAILLM)


def test_openai_wins_over_anthropic_when_both_keys_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    assert isinstance(get_llm(), OpenAILLM)


def test_agent_llm_env_forces_keyword_even_with_a_key_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("AGENT_LLM", "keyword")
    assert isinstance(get_llm(), KeywordLLM)


def test_agent_llm_env_forces_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("AGENT_LLM", "openai")
    assert isinstance(get_llm(), OpenAILLM)


def test_agent_llm_env_openai_without_a_key_degrades_to_keyword(monkeypatch):
    # AGENT_LLM=openai with no OPENAI_API_KEY at all: the openai SDK itself
    # refuses to construct a client with zero credentials configured — this
    # must degrade to KeywordLLM, never crash app start-up.
    monkeypatch.setenv("AGENT_LLM", "openai")
    assert isinstance(get_llm(), KeywordLLM)


def test_openai_llm_exposes_plan_translate_and_chat_step():
    llm = OpenAILLM(api_key="sk-fake")
    assert callable(llm.plan)
    assert callable(llm.translate)
    assert callable(llm.chat_step)


def test_keyword_llm_does_not_expose_chat_step():
    assert getattr(KeywordLLM(), "chat_step", None) is None


class TestReasoningEffort400SelfHealing:
    """Module M12: some newer OpenAI reasoning models 400 on
    /v1/chat/completions when function tools and a reasoning_effort other
    than "none" are combined ("Function tools with reasoning_effort are
    not supported for <model> in /v1/chat/completions ... set
    reasoning_effort to 'none'"). OpenAILLM must never hardcode which
    model names this applies to -- it recognizes the error TEXT, retries
    once with reasoning_effort="none", and remembers that per model name
    for the rest of the process."""

    @pytest.fixture(autouse=True)
    def _clean_cache(self, monkeypatch):
        monkeypatch.setattr(llm_module, "_REASONING_EFFORT_NONE_REQUIRED", set())

    def _make_failing_then_succeeding_client(self, model_name):
        """A fake `.chat.completions.create` that raises the exact 400 the
        first time reasoning_effort != "none" is passed, and succeeds
        (recording every call's kwargs) every other time."""
        calls = []

        class FakeMessage:
            content = "ok"
            tool_calls = []

        class FakeChoice:
            message = FakeMessage()

        class FakeResponse:
            choices = [FakeChoice()]

        def create(**kwargs):
            calls.append(kwargs)
            if kwargs.get("reasoning_effort") not in (None, "none"):
                raise Exception(
                    f"Error code: 400 - Function tools with reasoning_effort "
                    f"are not supported for {model_name} in "
                    "/v1/chat/completions. To use function tools, use "
                    "/v1/responses or set reasoning_effort to 'none'."
                )
            return FakeResponse()

        fake_completions = type("C", (), {"create": staticmethod(create)})()
        fake_chat = type("Chat", (), {"completions": fake_completions})()
        return type("Client", (), {"chat": fake_chat})(), calls

    def test_retries_once_with_reasoning_effort_none_and_caches_the_fix(self):
        llm = OpenAILLM(model="gpt-5.6-luna", api_key="sk-fake", reasoning_effort="high")
        fake_client, calls = self._make_failing_then_succeeding_client("gpt-5.6-luna")
        llm._client = fake_client

        result = llm._create_chat_completion(model="gpt-5.6-luna", messages=[])
        assert result.choices[0].message.content == "ok"
        assert len(calls) == 2
        assert calls[0]["reasoning_effort"] == "high"  # the failing attempt
        assert calls[1]["reasoning_effort"] == "none"  # the self-healed retry
        assert "gpt-5.6-luna" in llm_module._REASONING_EFFORT_NONE_REQUIRED

        # A second call skips straight to reasoning_effort="none" -- no
        # repeated failing attempt.
        calls.clear()
        llm._create_chat_completion(model="gpt-5.6-luna", messages=[])
        assert len(calls) == 1
        assert calls[0]["reasoning_effort"] == "none"

    def test_a_different_exception_is_not_swallowed(self):
        llm = OpenAILLM(model="gpt-5.6-luna", api_key="sk-fake", reasoning_effort="high")

        def create(**kwargs):
            raise Exception("Error code: 500 - internal server error")

        llm._client = type(
            "Client", (), {"chat": type("Chat", (), {"completions": type("C", (), {"create": staticmethod(create)})()})()}
        )()
        with pytest.raises(Exception, match="500"):
            llm._create_chat_completion(model="gpt-5.6-luna", messages=[])

    def test_unrelated_model_is_not_affected_by_another_models_cached_fix(self):
        # gpt-5.6-luna's self-heal must not leak into gpt-4o-mini's own
        # OpenAILLM instance -- the cache is keyed per model name.
        llm_module._REASONING_EFFORT_NONE_REQUIRED.add("gpt-5.6-luna")
        llm = OpenAILLM(model="gpt-4o-mini", api_key="sk-fake", reasoning_effort="high")
        fake_client, calls = self._make_failing_then_succeeding_client("gpt-4o-mini")
        llm._client = fake_client
        llm._create_chat_completion(model="gpt-4o-mini", messages=[])
        assert calls[0]["reasoning_effort"] == "high"  # not pre-emptively "none"
        assert calls[1]["reasoning_effort"] == "none"  # self-healed on this model too
