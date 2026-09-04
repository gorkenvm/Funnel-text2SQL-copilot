"""Tests for GET /api/ask/stream (M3a SSE endpoint).

The test process never has OPENAI_API_KEY/ANTHROPIC_API_KEY set (the
sandbox this suite runs in cannot reach either provider's API anyway),
so app.main's module-level ``llm`` is the deterministic KeywordLLM and
``agentic_agent`` is None — this test suite therefore exercises the
"keyless fallback" path of the SSE endpoint: plan + answer + done, no
tool-use trace. The autouse fixture below makes that guarantee explicit
rather than incidental.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _no_api_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _parse_sse(text: str) -> list[dict]:
    """Parse ``data: {...}\\n\\n`` / ``event: done\\ndata: {}\\n\\n`` frames."""
    events = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        if frame.startswith("event: done"):
            continue  # the terminal marker frame, not a trace event
        data_line = next((ln for ln in frame.splitlines() if ln.startswith("data:")), None)
        if data_line is None:
            continue
        events.append(json.loads(data_line[len("data:"):].strip()))
    return events


def test_stream_is_sse_content_type():
    resp = client.get("/api/ask/stream", params={"q": "Where is the biggest drop-off?", "lang": "en"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


def test_stream_yields_plan_then_answer_then_done():
    resp = client.get("/api/ask/stream", params={"q": "Weekly trend of test starts", "lang": "en"})
    assert resp.status_code == 200
    events = _parse_sse(resp.text)

    assert events, "expected at least one parsed SSE event"
    assert events[0]["type"] == "plan"
    assert "text" in events[0]

    answer_events = [e for e in events if e["type"] == "answer"]
    assert len(answer_events) == 1
    answer = answer_events[0]
    assert answer["answer"] and isinstance(answer["answer"], str)
    assert answer["rows"]
    assert answer["citations"] == []  # keyless fallback never cites knowledge

    # the literal "event: done" frame must be present and must be last
    assert "event: done" in resp.text
    assert resp.text.rstrip().endswith("data: {}")


def test_stream_has_no_agentic_trace_events_in_keyless_mode():
    resp = client.get("/api/ask/stream", params={"q": "Which channel completes best?", "lang": "en"})
    events = _parse_sse(resp.text)
    types = {e["type"] for e in events}
    # No tool_call/tool_result/sql/retry/chart events without a real provider.
    assert types <= {"plan", "answer", "error"}


def test_stream_rejects_missing_question():
    resp = client.get("/api/ask/stream")
    assert resp.status_code == 422


def test_stream_tr_lang_accepted():
    resp = client.get("/api/ask/stream", params={"q": "Weekly trend of test starts", "lang": "tr"})
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert any(e["type"] == "answer" for e in events)


class TestDashboardSseEventM11:
    """Module M11: a chat-style dashboard question, in keyless
    (KeywordLLM) mode, must emit a "dashboard" SSE event with cards +
    filter_label before the terminal "answer" — this is the exact SSE
    path the frontend's natural-language dashboard flow depends on."""

    def test_dashboard_question_emits_dashboard_event(self):
        resp = client.get(
            "/api/ask/stream",
            params={"q": "Build me a KPI dashboard for the last 3 months for Germany"},
        )
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        dashboard_events = [e for e in events if e["type"] == "dashboard"]
        assert len(dashboard_events) == 1
        dash = dashboard_events[0]
        assert dash["filter_label"] == "Last 3 months · DE"
        assert len(dash["cards"]) >= 10
        for card in dash["cards"]:
            assert {"key", "title", "chart", "rows", "answer", "sql"} <= set(card)
            assert card["sql"], f"{card['key']} has no sql"
        # M11 addendum 2: the keyless deterministic path never goes through
        # the agentic tool-call loop, so there is no call_id to correlate
        # against — the frontend falls back to a standalone trace step.
        assert not dash.get("call_id")

        # the dashboard event must precede the terminal answer, which is
        # a short paragraph mentioning the filter.
        types = [e["type"] for e in events]
        assert types.index("dashboard") < types.index("answer")
        answer_event = next(e for e in events if e["type"] == "answer")
        assert "Last 3 months" in answer_event["answer"]

    def test_non_dashboard_question_never_emits_dashboard_event(self):
        resp = client.get(
            "/api/ask/stream", params={"q": "Which channel completes best?"}
        )
        events = _parse_sse(resp.text)
        assert all(e["type"] != "dashboard" for e in events)


class TestStreamModelTierAndMemoryParams:
    """Module M7a: /api/ask/stream gains optional tier/session_id/memory
    query params. All optional and backward-compatible."""

    @pytest.mark.parametrize("tier", ["fast", "balanced", "max"])
    def test_valid_tier_accepted(self, tier):
        resp = client.get(
            "/api/ask/stream",
            params={"q": "Weekly trend of test starts", "tier": tier},
        )
        assert resp.status_code == 200
        assert any(e["type"] == "answer" for e in _parse_sse(resp.text))

    def test_invalid_tier_returns_422(self):
        resp = client.get(
            "/api/ask/stream",
            params={"q": "Weekly trend of test starts", "tier": "ultra"},
        )
        assert resp.status_code == 422

    def test_invalid_memory_value_returns_422(self):
        resp = client.get(
            "/api/ask/stream",
            params={"q": "Weekly trend of test starts", "memory": "maybe"},
        )
        assert resp.status_code == 422

    def test_session_id_and_memory_params_accepted_in_keyless_mode(self):
        resp = client.get(
            "/api/ask/stream",
            params={
                "q": "Weekly trend of test starts",
                "session_id": "stream-test-session",
                "memory": "off",
            },
        )
        assert resp.status_code == 200
        assert any(e["type"] == "answer" for e in _parse_sse(resp.text))

    def test_session_id_records_a_turn_even_in_keyless_fallback(self):
        from app.main import conversation_memory

        resp = client.get(
            "/api/ask/stream",
            params={
                "q": "Weekly trend of test starts",
                "session_id": "stream-record-session",
            },
        )
        assert resp.status_code == 200
        assert conversation_memory.has_turns("stream-record-session") is True
