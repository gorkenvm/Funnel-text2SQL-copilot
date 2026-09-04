"""Partial conversation memory (module M7a).

:class:`ConversationMemory` is a small, in-process, per-session store of
the last few conversational turns — used so a *referential* follow-up
question ("what about that?", "peki bunu?") can be answered with a hint
of what the previous turn was about, without ever handing the model raw
rows or a full previous answer back.

Deliberate privacy-by-design choices, all load-bearing for the tests in
``tests/test_memory.py``:

* Only a **structured** summary of each turn is kept — the question text
  itself, which tables its SQL touched (if any), a registered metric key
  (if any), and the *first line* of the answer, hard-truncated to 160
  characters. No raw database rows and no full answer text are ever
  stored.
* Sessions expire after ``ttl_seconds`` of inactivity (default 2h) and the
  store never holds more than ``max_sessions`` sessions at once (default
  200, evicted least-recently-used) — so a long-running process can never
  grow this store unboundedly.
* Injecting the stored context into a new question is gated by
  :func:`is_referential_question` — a deterministic, testable check — so
  an unrelated fresh question never gets contaminated by stale context
  (see :mod:`agent.agentic`'s use of this module for the activation gate
  and the "context" trace event).
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Optional

#: Turns kept per session (a small rolling window, oldest dropped first).
DEFAULT_MAX_TURNS = 5

#: A session with no activity for this long is treated as gone.
DEFAULT_TTL_SECONDS = 2 * 60 * 60  # 2 hours

#: Hard cap on concurrently tracked sessions (LRU-evicted beyond this).
DEFAULT_MAX_SESSIONS = 200

#: ``one_line_result`` is hard-truncated to this many characters.
MAX_ONE_LINE_RESULT_CHARS = 160

#: A question of this many words or fewer is treated as referential even
#: without matching a marker word — short follow-ups ("and by device?",
#: "ya iOS?") are almost always "continue the previous thread", not a
#: fresh, self-contained question.
SHORT_QUESTION_MAX_WORDS = 5

#: Small EN/TR marker list (word-boundary, case-insensitive) — a question
#: containing any of these is treated as referring back to prior context.
REFERENTIAL_MARKERS: tuple[str, ...] = (
    "that",
    "this",
    "it",
    "these",
    "those",
    "now",
    "also",
    "instead",
    "same",
    "again",
    "bunu",
    "şunu",
    "buna",
    "peki",
    "aynı",
    "şimdi",
    "onu",
    "bunlar",
)

_REFERENTIAL_RE = re.compile(
    r"\b(" + "|".join(re.escape(marker) for marker in REFERENTIAL_MARKERS) + r")\b",
    re.IGNORECASE | re.UNICODE,
)


def is_referential_question(question: str) -> bool:
    """True when ``question`` looks like it refers back to prior context.

    Deterministic activation gate (module M7a): either the question
    matches one of :data:`REFERENTIAL_MARKERS` (word-boundary,
    case-insensitive — so "notebook" does not falsely match "now", nor
    "shunted" match "unt"... actually neither of those are markers, but
    the point stands: word-boundary matching avoids substring false
    positives) or it is short (<= :data:`SHORT_QUESTION_MAX_WORDS` words).
    Both are strong, cheap-to-check signals of "continue the previous
    thread" rather than a fresh, self-contained question.
    """
    if not question or not question.strip():
        return False
    if _REFERENTIAL_RE.search(question):
        return True
    word_count = len(question.strip().split())
    return 0 < word_count <= SHORT_QUESTION_MAX_WORDS


def _one_line(text: str, limit: int = MAX_ONE_LINE_RESULT_CHARS) -> str:
    """First non-empty line of ``text``, hard-truncated to ``limit`` chars."""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    first_line = stripped.splitlines()[0]
    return first_line[:limit]


@dataclass
class Turn:
    """One structured, privacy-safe record of a past conversational turn."""

    question: str
    tables_used: list[str] = field(default_factory=list)
    metric_key: Optional[str] = None
    one_line_result: str = ""
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "tables_used": list(self.tables_used),
            "metric_key": self.metric_key,
            "one_line_result": self.one_line_result,
            "ts": self.ts,
        }


class ConversationMemory:
    """In-process, per-session store of the last few structured turns.

    Not thread-safe beyond what Python's GIL gives plain dict/deque
    mutation for free — matches every other in-process singleton in this
    codebase (e.g. :class:`agent.knowledge.KnowledgeBase`'s cache), which
    is fine for a single-process demo/small deployment.
    """

    def __init__(
        self,
        max_turns: int = DEFAULT_MAX_TURNS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
    ) -> None:
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        # An OrderedDict doubling as an LRU: re-touching a key moves it to
        # the end, so the front is always the least-recently-used session.
        self._sessions: "OrderedDict[str, deque[Turn]]" = OrderedDict()
        self._last_seen: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def _purge_expired(self, now: float) -> None:
        expired = [
            sid for sid, seen in self._last_seen.items() if now - seen > self.ttl_seconds
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
            self._last_seen.pop(sid, None)

    def _touch(self, session_id: str, now: float) -> None:
        self._last_seen[session_id] = now
        if session_id in self._sessions:
            self._sessions.move_to_end(session_id)

    def record(
        self,
        session_id: str,
        question: str,
        tables_used: Optional[list[str]] = None,
        metric_key: Optional[str] = None,
        one_line_result: str = "",
        now: Optional[float] = None,
    ) -> None:
        """Append one turn to ``session_id``'s history (oldest dropped past
        ``max_turns``). A falsy ``session_id`` is a silent no-op — callers
        never need to branch on "was a session_id given" themselves."""
        if not session_id:
            return
        now = time.time() if now is None else now
        self._purge_expired(now)

        if session_id not in self._sessions:
            while len(self._sessions) >= self.max_sessions:
                oldest_sid, _ = self._sessions.popitem(last=False)
                self._last_seen.pop(oldest_sid, None)
            self._sessions[session_id] = deque(maxlen=self.max_turns)

        self._sessions[session_id].append(
            Turn(
                question=question,
                tables_used=list(tables_used or []),
                metric_key=metric_key,
                one_line_result=_one_line(one_line_result),
                ts=now,
            )
        )
        self._touch(session_id, now)

    def get_turns(self, session_id: str, now: Optional[float] = None) -> list[dict]:
        """This session's turns, oldest first; ``[]`` if unknown/expired."""
        if not session_id:
            return []
        now = time.time() if now is None else now
        self._purge_expired(now)
        turns = self._sessions.get(session_id)
        if not turns:
            return []
        self._touch(session_id, now)
        return [t.to_dict() for t in turns]

    def has_turns(self, session_id: str, now: Optional[float] = None) -> bool:
        return bool(self.get_turns(session_id, now=now))


def format_context_block(turns: list[dict]) -> str:
    """Render stored turns as the "PRIOR CONTEXT" system-prompt block.

    Purely textual — never includes raw rows or a full previous answer,
    only what :class:`ConversationMemory` ever stored in the first place.
    """
    lines = ["PRIOR CONTEXT (most recent last):"]
    for turn in turns:
        tables = ", ".join(turn.get("tables_used") or []) or "n/a"
        metric = turn.get("metric_key") or "n/a"
        result = turn.get("one_line_result") or "n/a"
        lines.append(
            f"- Q: {turn.get('question', '')!r} | tables: {tables} | "
            f"metric: {metric} | result: {result}"
        )
    lines.append("Use this ONLY if the question refers to it; otherwise ignore.")
    return "\n".join(lines)
