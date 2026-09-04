"""Shared test fixtures for the ask-the-funnel agent."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Hermetic tests: never let a developer's real API keys (e.g. from a repo-root
# .env, which app.main auto-loads) switch the suite into a live-LLM provider.
# Everything here must run offline and deterministically.
os.environ["AGENT_LLM"] = "keyword"

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    # Lets tests import the M2 FastAPI app as `app.main` regardless of CWD.
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
GROUND_TRUTH = DATA_DIR / "_ground_truth.parquet"


@pytest.fixture(scope="session")
def driver():
    """One shared DuckDB driver for the whole test session."""
    from agent.db import DuckDBDriver

    return DuckDBDriver(data_dir=DATA_DIR)


@pytest.fixture(scope="session")
def registry():
    """The parsed metrics.yaml registry."""
    from agent.agent import _load_registry

    return _load_registry()
