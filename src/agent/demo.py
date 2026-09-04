"""End-to-end demo: eight canned English questions through the agent.

Run from the repo root with::

    PYTHONPATH=src python -m agent.demo

Uses the configured driver (AGENT_DB, default duckdb) and the configured
LLM (AnthropicLLM when ANTHROPIC_API_KEY is set, else the deterministic
KeywordLLM).
"""

from __future__ import annotations

from agent.agent import FunnelAgent
from agent.db import get_driver
from agent.llm import get_llm

DEMO_QUESTIONS: list[str] = [
    "Show me the funnel by market",
    "Where is the biggest drop-off?",
    "Which channel completes the hearing test best?",
    "Compare pairing rate iOS vs Android by market",
    "Which channel has the best D30 retention?",
    "How many users can we actually link across web and app?",
    "Weekly trend of test starts",
    "Which channel drives downloads vs actual pairings?",
]


def _fmt_cell(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4f}" if abs(value) < 10 else f"{value:,.1f}"
    return str(value)


def run_demo() -> None:
    """Run the eight demo questions and print a compact report."""
    agent = FunnelAgent(driver=get_driver(), llm=get_llm())

    print("=" * 78)
    print("ASK-THE-FUNNEL DEMO — planner:", type(agent.llm).__name__)
    print("=" * 78)

    for i, question in enumerate(DEMO_QUESTIONS, start=1):
        result = agent.ask(question)
        print(f"\nQ{i}. {question}")
        print(f"    mode={result['mode']}"
              + (f"  metric={result.get('metric_key')}" if result.get("metric_key") else ""))
        rows = result["rows"]
        if rows:
            columns = list(rows[0].keys())
            print("    " + " | ".join(columns))
            for row in rows[:6]:
                print("    " + " | ".join(_fmt_cell(row[c]) for c in columns))
            if len(rows) > 6:
                print(f"    ... ({len(rows)} rows total)")
        chart = result.get("chart")
        if chart:
            spec = f"{chart['type']} x={chart.get('x')} y={chart.get('y')}"
            if chart.get("series"):
                spec += f" series={chart['series']}"
            print(f"    chart: {spec}")
        print(f"    A: {result['answer']}")

    print("\n" + "=" * 78)
    print("Registered KPIs:")
    for metric in agent.list_metrics():
        print(f"  - {metric['key']}: {metric['title']}")


if __name__ == "__main__":
    run_demo()
