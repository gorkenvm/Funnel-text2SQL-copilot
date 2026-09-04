"""Unit tests for agent.memory (module M7a: partial conversation memory).

Pure unit tests — no LLM, no driver, no network — for the activation gate
(is_referential_question), the structured store's TTL/LRU behaviour, and
the PRIOR CONTEXT prompt-block formatting. See tests/test_agentic.py's
TestConversationMemoryIntegration for the end-to-end ScriptedLLM proof
that this module is actually wired into the agentic loop correctly.
"""

from __future__ import annotations

from agent.memory import (
    ConversationMemory,
    Turn,
    format_context_block,
    is_referential_question,
)


class TestIsReferentialQuestion:
    def test_empty_question_is_not_referential(self):
        assert is_referential_question("") is False
        assert is_referential_question("   ") is False

    def test_marker_words_are_referential_en(self):
        for q in [
            "What about that channel?",
            "Break this down by device",
            "Show it as a chart",
            "What about those users",
            "Can you also add retention",
            "Use the same filter instead",
            "Run it again for last week",
        ]:
            assert is_referential_question(q) is True, q

    def test_marker_words_are_referential_tr(self):
        for q in [
            "Peki bunu kanal bazında gösterir misin",
            "Şunu cihaza göre kırar mısın",
            "Buna göre bir de trend çıkar",
            "Aynı filtreyi uygula",
            "Şimdi de app tarafını göster",
            "Onu markete göre böl",
        ]:
            assert is_referential_question(q) is True, q

    def test_short_question_is_referential_even_without_a_marker(self):
        assert is_referential_question("And by device?") is True
        assert is_referential_question("Ya iOS?") is True
        assert is_referential_question("Weekly trend") is True  # 2 words

    def test_word_boundary_avoids_false_positive_substrings(self):
        # "now" must not match inside "notebook", "it" must not match
        # inside "biting" — word-boundary regex, not substring search.
        # Both examples are also > 5 words, so the short-question
        # fallback does not accidentally make them referential either.
        long_non_referential = (
            "Please export the raw notebook analysis workbook file for review"
        )
        assert len(long_non_referential.split()) > 5
        assert is_referential_question(long_non_referential) is False

    def test_independent_long_question_is_not_referential(self):
        q = "Compare hearing aid pairing rate by acquisition channel across markets"
        assert len(q.split()) > 5
        assert is_referential_question(q) is False


class TestConversationMemoryRecordAndRetrieve:
    def test_unknown_session_returns_no_turns(self):
        memory = ConversationMemory()
        assert memory.get_turns("nope") == []
        assert memory.has_turns("nope") is False

    def test_record_then_get_turns_roundtrips_structured_fields(self):
        memory = ConversationMemory()
        memory.record(
            session_id="s1",
            question="Where is the biggest drop-off?",
            tables_used=["gold.step_conversion"],
            metric_key="step_conversion_rates",
            one_line_result="Highest drop-off: complete -> download at 32%.",
            now=1000.0,
        )
        turns = memory.get_turns("s1", now=1000.0)
        assert len(turns) == 1
        turn = turns[0]
        assert turn["question"] == "Where is the biggest drop-off?"
        assert turn["tables_used"] == ["gold.step_conversion"]
        assert turn["metric_key"] == "step_conversion_rates"
        assert turn["one_line_result"] == "Highest drop-off: complete -> download at 32%."
        assert turn["ts"] == 1000.0

    def test_no_raw_rows_or_full_answer_stored(self):
        # Structural guarantee: Turn simply has no field for raw rows or a
        # full answer — recording never has anywhere to put them.
        turn = Turn(question="q")
        assert set(turn.to_dict()) == {"question", "tables_used", "metric_key", "one_line_result", "ts"}

    def test_one_line_result_is_first_line_only(self):
        memory = ConversationMemory()
        memory.record(
            session_id="s1",
            question="q",
            one_line_result="First line.\nSecond line with detail.\nThird.",
            now=1.0,
        )
        assert memory.get_turns("s1", now=1.0)[0]["one_line_result"] == "First line."

    def test_one_line_result_is_truncated_to_160_chars(self):
        memory = ConversationMemory()
        long_answer = "x" * 500
        memory.record(session_id="s1", question="q", one_line_result=long_answer, now=1.0)
        stored = memory.get_turns("s1", now=1.0)[0]["one_line_result"]
        assert len(stored) == 160
        assert stored == "x" * 160

    def test_missing_session_id_is_a_silent_no_op(self):
        memory = ConversationMemory()
        memory.record(session_id="", question="q", now=1.0)
        assert len(memory) == 0

    def test_keeps_only_last_max_turns(self):
        memory = ConversationMemory(max_turns=3)
        for i in range(5):
            memory.record(session_id="s1", question=f"q{i}", now=float(i))
        turns = memory.get_turns("s1", now=10.0)
        assert [t["question"] for t in turns] == ["q2", "q3", "q4"]


class TestConversationMemoryTTL:
    def test_session_expires_after_ttl(self):
        # Reading a session counts as activity too (see
        # test_activity_refreshes_ttl below), so this checks expiry in one
        # shot past the TTL rather than probing mid-window first.
        memory = ConversationMemory(ttl_seconds=100.0)
        memory.record(session_id="s1", question="q", now=0.0)
        assert memory.has_turns("s1", now=101.0) is False

    def test_activity_refreshes_ttl(self):
        memory = ConversationMemory(ttl_seconds=100.0)
        memory.record(session_id="s1", question="q1", now=0.0)
        # Touching the session at t=90 (still alive) should push its
        # expiry to 190, not leave it expiring at the original 100.
        memory.record(session_id="s1", question="q2", now=90.0)
        assert memory.has_turns("s1", now=150.0) is True

    def test_expired_session_is_purged_from_the_store(self):
        memory = ConversationMemory(ttl_seconds=10.0)
        memory.record(session_id="s1", question="q", now=0.0)
        assert len(memory) == 1
        memory.get_turns("s1", now=100.0)  # triggers purge as a side effect
        assert len(memory) == 0


class TestConversationMemoryLRU:
    def test_evicts_least_recently_used_session_beyond_cap(self):
        memory = ConversationMemory(max_sessions=2)
        memory.record(session_id="a", question="q", now=0.0)
        memory.record(session_id="b", question="q", now=1.0)
        memory.record(session_id="c", question="q", now=2.0)  # evicts "a"
        assert memory.has_turns("a", now=2.0) is False
        assert memory.has_turns("b", now=2.0) is True
        assert memory.has_turns("c", now=2.0) is True
        assert len(memory) == 2

    def test_reading_a_session_counts_as_recent_use(self):
        memory = ConversationMemory(max_sessions=2)
        memory.record(session_id="a", question="q", now=0.0)
        memory.record(session_id="b", question="q", now=1.0)
        memory.get_turns("a", now=2.0)  # "a" is now more-recently-used than "b"
        memory.record(session_id="c", question="q", now=3.0)  # evicts "b", not "a"
        assert memory.has_turns("a", now=3.0) is True
        assert memory.has_turns("b", now=3.0) is False


class TestFormatContextBlock:
    def test_includes_marker_title_and_use_only_if_referring_instruction(self):
        block = format_context_block(
            [{"question": "q", "tables_used": [], "metric_key": None, "one_line_result": "r", "ts": 0.0}]
        )
        assert "PRIOR CONTEXT" in block
        assert "Use this ONLY if the question refers to it" in block

    def test_renders_every_turn(self):
        turns = [
            {"question": "q1", "tables_used": ["web_events"], "metric_key": None, "one_line_result": "r1", "ts": 0.0},
            {"question": "q2", "tables_used": [], "metric_key": "funnel_overview", "one_line_result": "r2", "ts": 1.0},
        ]
        block = format_context_block(turns)
        assert "q1" in block and "q2" in block
        assert "web_events" in block
        assert "funnel_overview" in block

    def test_empty_turns_still_renders_the_instruction(self):
        block = format_context_block([])
        assert "Use this ONLY if the question refers to it" in block
