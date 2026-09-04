"""Lightweight RAG over the analyst knowledge base (``docs/knowledge/*.md``).

Two retrieval backends, selected automatically:

* **OpenAI embeddings** (``text-embedding-3-small``) with an on-disk JSON
  cache, used when ``OPENAI_API_KEY`` is set and the API call succeeds.
* **BM25-style TF-IDF fallback** — a small, dependency-free pure-Python
  index — used whenever the embedding backend is unavailable or fails
  (no key, no network, or any API error). This is the path the test
  suite always exercises, since outbound calls to the OpenAI API are not
  reachable from the sandbox the tests run in.

No vector database and no heavy ML dependency (e.g. sentence-transformers)
are used, by design — the knowledge base is a handful of short markdown
files, not a corpus that needs one.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Default location of the knowledge markdown files: <repo>/docs/knowledge.
_DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "docs" / "knowledge"

#: Soft cap on words per chunk before a heading section is split further.
MAX_CHUNK_WORDS = 300

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_EMBEDDING_MODEL = "text-embedding-3-small"


@dataclass(frozen=True)
class Chunk:
    """One retrievable passage: a markdown heading section (or a slice of one)."""

    source_file: str
    heading: str
    text: str


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def chunk_markdown(path: Path, max_words: int = MAX_CHUNK_WORDS) -> list[Chunk]:
    """Split one markdown file into heading-anchored chunks.

    A new chunk starts at every heading line (``#`` through ``######``).
    Text before the first heading is attributed to the file's own name.
    A section whose body exceeds ``max_words`` is further split on
    paragraph breaks (blank lines) into ``"<heading> (part N)"`` pieces,
    so no single chunk drifts far past the word budget while headings
    stay in the citation.
    """
    text = path.read_text(encoding="utf-8")
    sections: list[tuple[str, list[str]]] = []
    current_heading = path.stem
    current_lines: list[str] = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            if current_lines:
                sections.append((current_heading, current_lines))
            current_heading = match.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_heading, current_lines))

    chunks: list[Chunk] = []
    for heading, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        if len(body.split()) <= max_words:
            chunks.append(Chunk(source_file=path.name, heading=heading, text=body))
            continue

        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        bucket: list[str] = []
        bucket_words = 0
        part = 1
        parts: list[str] = []
        for para in paragraphs:
            n_words = len(para.split())
            if bucket and bucket_words + n_words > max_words:
                parts.append("\n\n".join(bucket))
                bucket, bucket_words = [], 0
            bucket.append(para)
            bucket_words += n_words
        if bucket:
            parts.append("\n\n".join(bucket))

        for part_text in parts:
            part_heading = heading if len(parts) == 1 else f"{heading} (part {part})"
            chunks.append(Chunk(source_file=path.name, heading=part_heading, text=part_text))
            part += 1
    return chunks


class _Bm25Index:
    """Minimal, dependency-free Okapi BM25 index over a fixed chunk list."""

    K1 = 1.5
    B = 0.75

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self._doc_tokens = [_tokenize(f"{c.heading} {c.text}") for c in chunks]
        self._doc_len = [len(toks) for toks in self._doc_tokens]
        self._avg_len = (sum(self._doc_len) / len(self._doc_len)) if self._doc_tokens else 0.0
        self._df: Counter[str] = Counter()
        for tokens in self._doc_tokens:
            self._df.update(set(tokens))
        self._n_docs = len(chunks)

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log(1.0 + (self._n_docs - df + 0.5) / (df + 0.5))

    def search(self, query: str, k: int) -> list[tuple[Chunk, float]]:
        query_tokens = _tokenize(query)
        if not query_tokens or not self.chunks:
            return []
        scored: list[tuple[Chunk, float]] = []
        for idx, tokens in enumerate(self._doc_tokens):
            term_freq = Counter(tokens)
            doc_len = self._doc_len[idx]
            score = 0.0
            for term in query_tokens:
                freq = term_freq.get(term, 0)
                if freq == 0:
                    continue
                idf = self._idf(term)
                denom = freq + self.K1 * (1 - self.B + self.B * doc_len / (self._avg_len or 1.0))
                score += idf * (freq * (self.K1 + 1)) / (denom or 1.0)
            if score > 0:
                scored.append((self.chunks[idx], score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]


class KnowledgeBase:
    """Loads ``docs/knowledge/*.md``, chunks them, and serves ``search()``."""

    def __init__(
        self,
        directory: Optional[Path] = None,
        cache_path: Optional[Path] = None,
    ) -> None:
        self.directory = Path(directory) if directory else _DEFAULT_KNOWLEDGE_DIR
        self.cache_path = (
            Path(cache_path) if cache_path else self.directory / ".embedding_cache.json"
        )
        self.chunks: list[Chunk] = []
        if self.directory.exists():
            for path in sorted(self.directory.glob("*.md")):
                self.chunks.extend(chunk_markdown(path))
        self._bm25 = _Bm25Index(self.chunks)
        self._embedding_cache: Optional[dict[str, list[float]]] = None
        self._embedding_backend_failed = False

    # ------------------------------------------------------------- search

    def search(self, query: str, k: int = 3) -> list[dict]:
        """Return up to ``k`` best-matching chunks, most relevant first.

        Each result is ``{"source_file", "heading", "text", "score"}`` —
        ready to cite and to feed back to an LLM as a tool result.
        """
        if os.environ.get("OPENAI_API_KEY") and not self._embedding_backend_failed:
            try:
                return self._search_embeddings(query, k)
            except Exception:  # noqa: BLE001 - network/API is optional, fall back silently
                self._embedding_backend_failed = True
        return self._search_bm25(query, k)

    def _search_bm25(self, query: str, k: int) -> list[dict]:
        hits = self._bm25.search(query, k)
        return [self._as_result(chunk, score) for chunk, score in hits]

    def _search_embeddings(self, query: str, k: int) -> list[dict]:
        import openai  # lazy import by design

        client = openai.OpenAI()
        vectors = self._load_or_build_embeddings(client)
        query_vector = self._embed([query], client)[0]

        scored: list[tuple[Chunk, float]] = []
        for chunk in self.chunks:
            vector = vectors.get(self._chunk_key(chunk))
            if vector is None:
                continue
            scored.append((chunk, _cosine_similarity(query_vector, vector)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [self._as_result(chunk, score) for chunk, score in scored[:k]]

    @staticmethod
    def _as_result(chunk: Chunk, score: float) -> dict:
        return {
            "source_file": chunk.source_file,
            "heading": chunk.heading,
            "text": chunk.text,
            "score": round(float(score), 4),
        }

    # ------------------------------------------------------- embedding cache

    def _chunk_key(self, chunk: Chunk) -> str:
        digest = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()[:16]
        return f"{chunk.source_file}::{chunk.heading}::{digest}"

    def _load_or_build_embeddings(self, client) -> dict[str, list[float]]:
        if self._embedding_cache is not None:
            return self._embedding_cache
        cache: dict[str, list[float]] = {}
        if self.cache_path.exists():
            try:
                cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cache = {}
        missing = [c for c in self.chunks if self._chunk_key(c) not in cache]
        if missing:
            vectors = self._embed([c.text for c in missing], client)
            for chunk, vector in zip(missing, vectors):
                cache[self._chunk_key(chunk)] = vector
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(cache), encoding="utf-8")
            except OSError:
                pass  # cache is a pure optimisation; a write failure is not fatal
        self._embedding_cache = cache
        return cache

    @staticmethod
    def _embed(texts: list[str], client) -> list[list[float]]:
        response = client.embeddings.create(model=_EMBEDDING_MODEL, input=texts)
        return [item.embedding for item in response.data]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0
