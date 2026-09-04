"""Deterministic test double for the agentic loop.

:class:`ScriptedLLM` implements the same ``chat_step`` protocol
:class:`agent.agentic.AgenticFunnelAgent` expects from a real provider,
but replays a fixed, hand-written script instead of calling any network
API. This is how ``test_agentic.py`` proves the tool-use loop end to end
— including SQL self-correction — without ever touching a real LLM.

Not imported by application code (``app/main.py``); it exists purely for
tests, so it lives outside the ``agent`` package's runtime surface but
inside the package for easy importing (``from agent.testing import
ScriptedLLM``).
"""

from __future__ import annotations

from typing import Callable, Sequence, Union

#: A scripted turn is either a literal turn dict, or a callable that
#: inspects the conversation so far and *decides* what to return — used
#: for turns that must react to an earlier tool result (e.g. only emit
#: corrected SQL after seeing the previous attempt's error message).
ScriptedTurn = Union[dict, Callable[[list[dict], list[dict]], dict]]


class ScriptedLLMExhausted(AssertionError):
    """Raised when the loop asks for more turns than the script provides."""


class ScriptedLLM:
    """Replays a fixed list of chat turns, one per ``chat_step`` call.

    Each element of ``turns`` is either:

    * a literal dict, e.g. ``{"content": "final answer", "tool_calls": []}``
      or ``{"tool_calls": [{"id": "c1", "name": "get_schema", "arguments": {}}]}``
      (``content``/``tool_calls`` default to ``None``/``[]`` if omitted); or
    * a callable ``(messages, tools) -> dict`` for a turn that needs to
      look at the conversation so far.

    Every call is recorded in ``self.calls`` for test assertions.
    """

    def __init__(self, turns: Sequence[ScriptedTurn]) -> None:
        self._turns = list(turns)
        self._index = 0
        self.calls: list[dict] = []

    def chat_step(self, messages: list[dict], tools: list[dict]) -> dict:
        if self._index >= len(self._turns):
            raise ScriptedLLMExhausted(
                f"ScriptedLLM was asked for a turn beyond its "
                f"{len(self._turns)} scripted turns — the loop under test "
                "made more chat_step calls than the script anticipated."
            )
        turn = self._turns[self._index]
        self._index += 1
        result = dict(turn(messages, tools)) if callable(turn) else dict(turn)
        result.setdefault("content", None)
        result.setdefault("tool_calls", [])
        self.calls.append({"messages": list(messages), "tools": tools, "result": result})
        return result

    def plan(self, question: str, schema_doc: str, metric_keys: Sequence[str]) -> dict:
        """Minimal :class:`~agent.llm.LLMClient` compatibility.

        ``AgenticFunnelAgent`` never calls this — only ``chat_step`` — but
        keeping it means a ``ScriptedLLM`` can also stand in anywhere an
        ``LLMClient`` is expected.
        """
        return {
            "mode": "clarify",
            "narrative_hint": "ScriptedLLM does not implement plan(); use chat_step().",
        }


def dashboard_script(question: str, final_answer: str = "Here is the filtered KPI dashboard.") -> list[dict]:
    """Module M11: a deterministic two-turn :class:`ScriptedLLM` script
    exercising the ``build_dashboard`` tool exactly as a real tool-calling
    LLM would after reading the M11 dashboard-intent system-prompt rule —
    turn 1 calls ``build_dashboard`` with filters regex-parsed from
    ``question`` (:func:`agent.dashboard.extract_filters_from_text`, the
    same deterministic parser :class:`agent.llm.KeywordLLM` uses), turn 2
    is a short final answer with no further tool calls.

    Lets ``test_agentic.py`` (and any other caller) prove the agentic
    "dashboard" SSE event end to end without a real LLM: ``ScriptedLLM
    (dashboard_script(q))``.
    """
    from agent.dashboard import extract_filters_from_text

    extracted = extract_filters_from_text(question)
    arguments: dict = {}
    if extracted.get("relative_range_text"):
        arguments["relative_range"] = extracted["relative_range_text"]
    for field in ("market", "channel", "device", "platform"):
        if extracted.get(field):
            arguments[field] = extracted[field]
    return [
        {"tool_calls": [{"id": "c1", "name": "build_dashboard", "arguments": arguments}]},
        {"content": final_answer, "tool_calls": []},
    ]
