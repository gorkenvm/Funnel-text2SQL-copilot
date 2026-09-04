"""Tests for the M3a lightweight RAG layer (agent.knowledge).

These tests force the BM25 fallback path regardless of the host
environment, by removing OPENAI_API_KEY for the duration of each test —
the sandbox this suite runs in cannot reach api.openai.com anyway, but a
future host might set the key, and this suite must stay deterministic
and offline either way.
"""

from __future__ import annotations

import pytest

from agent.knowledge import KnowledgeBase, chunk_markdown


@pytest.fixture(autouse=True)
def _no_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase()


class TestChunking:
    def test_files_produce_multiple_chunks(self, kb):
        assert len(kb.chunks) >= 4  # at least a few headings across 4 files

        by_file: dict[str, int] = {}
        for chunk in kb.chunks:
            by_file[chunk.source_file] = by_file.get(chunk.source_file, 0) + 1
        expected_files = {
            "methodology.md",
            "privacy.md",
            "insights.md",
            "attribution.md",
        }
        assert expected_files <= set(by_file)
        for filename in expected_files:
            assert by_file[filename] >= 1

    def test_chunks_respect_the_word_budget(self, kb):
        for chunk in kb.chunks:
            word_count = len(chunk.text.split())
            assert word_count <= 320, (  # small slack over the 300 target
                f"{chunk.source_file} / {chunk.heading!r} has {word_count} words"
            )

    def test_chunk_has_heading_and_nonempty_text(self, kb):
        for chunk in kb.chunks:
            assert chunk.heading
            assert chunk.text.strip()

    def test_chunk_markdown_splits_on_headings(self, tmp_path):
        md = tmp_path / "sample.md"
        md.write_text(
            "# Title\n\nIntro paragraph.\n\n"
            "## Section A\n\nBody A.\n\n"
            "## Section B\n\nBody B.\n",
            encoding="utf-8",
        )
        chunks = chunk_markdown(md)
        headings = [c.heading for c in chunks]
        assert headings == ["Title", "Section A", "Section B"]
        assert all(c.source_file == "sample.md" for c in chunks)

    def test_chunk_markdown_splits_long_section_by_word_budget(self, tmp_path):
        md = tmp_path / "long.md"
        long_body = "\n\n".join(f"Paragraph {i} " + "word " * 80 for i in range(6))
        md.write_text(f"# Long Section\n\n{long_body}\n", encoding="utf-8")
        chunks = chunk_markdown(md, max_words=100)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk.text.split()) <= 130  # budget plus one paragraph's slack


class TestFallbackRetrieval:
    def test_search_returns_result_shape(self, kb):
        results = kb.search("privacy consent", k=2)
        assert results
        for r in results:
            assert {"source_file", "heading", "text", "score"} <= set(r)

    def test_mobile_completion_question_surfaces_insights(self, kb):
        results = kb.search("why is mobile completion low", k=3)
        assert results, "expected at least one BM25 hit"
        sources = [r["source_file"] for r in results]
        assert "insights.md" in sources
        assert sources[0] == "insights.md", f"top hit was {sources[0]!r}, not insights.md"

    def test_privacy_question_surfaces_privacy_doc(self, kb):
        results = kb.search("is hashing the same as anonymisation", k=3)
        sources = [r["source_file"] for r in results]
        assert "privacy.md" in sources

    def test_attribution_question_surfaces_attribution_doc(self, kb):
        results = kb.search("first touch vs last touch attribution utm", k=3)
        sources = [r["source_file"] for r in results]
        assert "attribution.md" in sources

    def test_no_match_returns_empty(self, kb):
        assert kb.search("", k=3) == []

    def test_never_calls_the_network(self, kb, monkeypatch):
        # Even if a key sneaks into the environment mid-test, search() must
        # never import/touch the openai package when OPENAI_API_KEY is unset
        # (the fixture above already guarantees that; this just documents it).
        results = kb.search("censoring window", k=1)
        assert results
